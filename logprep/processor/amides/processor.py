"""
Amides
======

The :code:`Amides` processor implements the proof-of-concept Adaptive Misuse Detection System
(AMIDES). AMIDES extends conventional rule matching of SIEM systems by applying machine learning
components aiming to detect attacks that evade existing SIEM rules as well as otherwise undetected
attack variants. It learns from SIEM rules and historical benign events and can thus estimate which
SIEM rule was tried to be evaded. An overview of AMIDES is depicted in the figure below.


.. figure:: ../../_images/amides.svg
    :align: center

    Overview of the AMIDES architecture.


The machine learning components of AMIDES are trained using the current SIEM rule set and
historical benign events. Incoming events are transformed into feature vectors by the feature
extraction component. During operation, features learned during the training phase will be
re-used by the feature extraction component.
Feature vectors are then passed to the Misuse Classification component which classifies events as
malicious or benign. In case of a malicious result, the feature vector is passed to the Rule
Attribution component which generates a ranked list of SIEM rules potentially evaded by the event.
Finally, results generated by the Rule Attribution component and conventional rule matching results
can be correlated for alert generation.

Since there is a plethora of different SIEM event types, the current implementation focuses on
events that provide process command lines. Command lines are most commonly targeted by SIEM rules
while they are also highly vulnerable to evasions. The rules and models for AMIDES provided in the
quickstart example are for Sysmon Process Creation events. In general, the Amides rule format allows
to create rules for other event types that provide process command lines, e.g. Process Creation
events generated by Windows Security Auditing.

Misuse classification is performed by the :code:`MisuseDetector` class. Instances of the
:code:`MisuseDetector` contain the model for misuse classification, which includes the trained
classifier instance, the corresponding feature extractor, and an additional scaler to transform
classifier results into the pre-defined output range between 0 and 1. The processor configuration
parameter :code:`decision_threshold` is used to fine-tune the classification results produced by the
misuse detector.

Rule attribution is performed by the :code:`RuleAttributor` class. The :code:`num_rule_attributions`
configuration parameter determines the number of rule attributions returned by the attributor.
Models and vectorizer for rule attribution and feature extraction are held by :code:`RuleAttributor`
instances.

In order to speed up the detection and attribution process, the :code:`Amides` processor makes use
of a LRU cache that keeps track of incoming command line samples. In case of a previously seen
command line, classification and attribution results can be retrieved from the cache in a shorter
amount of time. The :code:`max_cache_entries` configuration parameter determines the maximum number
of elements of the internal cache.

Models used by the `MisuseDetector` and `RuleAttributor` are currently generated by `scikit-learn`.
Each trained model needs to be packed into a dictionary together with its corresponding feature
extractor and scaler. Dictionaries are then pickled and compressed (.zip). The URI or path of the
compressed models file is given by the :code:`models_path` configuration parameter. An example of a
configuration of the :code:`Amides` processor is given below:

Processor Configuration
^^^^^^^^^^^^^^^^^^^^^^^
..  code-block:: yaml
    :linenos:

    - amides:
        type: amides
        specific_rules:
            - tests/testdata/rules/specific/
        generic_rules:
            - tests/testdata/rules/generic/
        max_cache_entries: 10000
        decision_threshold: 0.0
        num_rule_attributions: 10

To keep track of the components performance, the :code:`Amides` processor tracks several processor
metrics. This includes the mean misuse detection time, the mean rule attribution time, and several
cache-related metrics like the number of hits and misses and the current cache load.

.. autoclass:: logprep.processor.amides.processor.Amides.Config
   :members:
   :undoc-members:
   :inherited-members:
   :noindex:

.. automodule:: logprep.processor.amides.rule
"""

import logging
from functools import cached_property, lru_cache
from multiprocessing import current_process
from pathlib import Path
from typing import List, Tuple
from zipfile import ZipFile

import joblib
from attr import define, field, validators

from logprep.abc.processor import Processor
from logprep.metrics.metrics import CounterMetric, GaugeMetric, HistogramMetric, Metric
from logprep.processor.amides.detection import MisuseDetector, RuleAttributor
from logprep.processor.amides.normalize import CommandLineNormalizer
from logprep.processor.amides.rule import AmidesRule
from logprep.util.getter import GetterFactory
from logprep.util.helper import get_dotted_field_value

logger = logging.getLogger("Amides")


class Amides(Processor):
    """Proof-of-concept implementation of the Adaptive Misuse Detection System (AMIDES)."""

    @define(kw_only=True)
    class Config(Processor.Config):
        """Amides processor configuration class."""

        max_cache_entries: int = field(default=1048576, validator=validators.instance_of(int))
        """Maximum number of cached command lines  and their rule attribution results."""
        decision_threshold: float = field(validator=validators.instance_of(float))
        """Specifies the decision threshold of the misuse detector to adjust it's overall
        classification performance."""
        num_rule_attributions: int = field(default=10, validator=validators.instance_of(int))
        """Number of rule attributions returned in case of a positive misuse detection result."""
        models_path: str = field(validator=validators.instance_of(str))
        """
        Path or URI of the archive (.zip) containing the models used by the misuse detector
        and the rule attributor.

        .. security-best-practice::
           :title: Processor - Amides Model

           Ensure that you only use models from trusted sources, as it can be used to inject python
           code into the runtime.
        """

    @define(kw_only=True)
    class Metrics(Processor.Metrics):
        """Track statistics specific for Amides processor instances."""

        total_cmdlines: CounterMetric = field(
            factory=lambda: CounterMetric(
                description="Total number of command lines processed.",
                name="amides_total_cmdlines",
            )
        )
        """Total number of command lines processed."""
        new_results: GaugeMetric = field(
            factory=lambda: GaugeMetric(
                description="Number of command lines that triggered detection and rule attribution.",
                name="amides_new_results",
            )
        )
        """Number of command lines that triggered detection and rule attribution."""
        cached_results: GaugeMetric = field(
            factory=lambda: GaugeMetric(
                description="Number of command lines that could be resolved from cache.",
                name="amides_cached_results",
            )
        )
        """Number of command lines that could be resolved from cache."""
        num_cache_entries: GaugeMetric = field(
            factory=lambda: GaugeMetric(
                description="Absolute number of current cache entries.",
                name="amides_num_cache_entries",
            )
        )
        """Absolute number of current cache entries."""
        cache_load: GaugeMetric = field(
            factory=lambda: GaugeMetric(
                description="Relative cache load.",
                name="amides_cache_load",
            )
        )
        """Relative cache load."""
        mean_misuse_detection_time: HistogramMetric = field(
            factory=lambda: HistogramMetric(
                description="Mean processing time of command lines classified by the misuse detector.",
                name="amides_mean_misuse_detection_time",
            )
        )
        """Mean processing time of command lines classified by the misuse detector."""
        mean_rule_attribution_time: HistogramMetric = field(
            factory=lambda: HistogramMetric(
                description="Mean processing time of command lines attributed by the rule attributor.",
                name="amides_mean_rule_attribution_time",
            )
        )
        """Mean processing time of command lines attributed by the rule attributor."""

    __slots__ = (
        "_misuse_detector",
        "_rule_attributor",
    )

    _misuse_detector: MisuseDetector
    _rule_attributor: RuleAttributor

    rule_class = AmidesRule

    @cached_property
    def _normalizer(self):
        return CommandLineNormalizer(max_num_values_length=3, max_str_length=30)

    @cached_property
    def _evaluate_cmdline_cached(self):
        return lru_cache(maxsize=self._config.max_cache_entries)(self._evaluate_cmdline)

    def setup(self):
        super().setup()
        models = self._load_and_unpack_models()

        self._misuse_detector = MisuseDetector(models["single"], self._config.decision_threshold)
        self._rule_attributor = RuleAttributor(
            models["multi"],
            self._config.num_rule_attributions,
        )

    def _load_and_unpack_models(self):
        if not Path(self._config.models_path).exists():
            logger.debug("Getting AMIDES models archive...")
            models_archive = Path(f"{current_process().name}-{self.name}.zip")
            models_archive.touch()
            models_archive.write_bytes(
                GetterFactory.from_string(str(self._config.models_path)).get_raw()
            )
            logger.debug("Finished getting AMIDES models archive...")
            self._config.models_path = str(models_archive.absolute())

        with ZipFile(self._config.models_path, mode="r") as zip_file:
            with zip_file.open("model", mode="r") as models_file:
                models = joblib.load(models_file)

        return models

    def _apply_rules(self, event: dict, rule: AmidesRule):
        cmdline = get_dotted_field_value(event, rule.source_fields[0])
        if not cmdline:
            return

        self.metrics.total_cmdlines += 1

        normalized = self._normalizer.normalize(cmdline)
        if not normalized:
            return

        result = self._evaluate_cmdline_cached(normalized)
        self._update_cache_metrics()

        self._write_target_field(event=event, rule=rule, result=result)

    def _evaluate_cmdline(self, cmdline: str):
        result = {}

        malicious, result["confidence"] = self._perform_misuse_detection(cmdline)
        if malicious:
            result["attributions"] = self._calculate_rule_attributions(cmdline)

        return result

    @Metric.measure_time(metric_name="mean_misuse_detection_time")
    def _perform_misuse_detection(self, cmdline: str) -> Tuple[bool, float]:
        result = self._misuse_detector.detect(cmdline)
        return result

    @Metric.measure_time(metric_name="mean_rule_attribution_time")
    def _calculate_rule_attributions(self, cmdline: str) -> List[dict]:
        attributions = self._rule_attributor.attribute(cmdline)
        return attributions

    def _update_cache_metrics(self):
        cache_info = self._evaluate_cmdline_cached.cache_info()
        self.metrics.new_results += cache_info.misses
        self.metrics.cached_results += cache_info.hits
        self.metrics.num_cache_entries += cache_info.currsize
        self.metrics.cache_load += cache_info.currsize / cache_info.maxsize
