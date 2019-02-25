from __future__ import unicode_literals

import json
import logging
from pathlib import Path
from builtins import str

from future.utils import iteritems

from snips_nlu_utils import normalize

from snips_nlu.common.log_utils import log_elapsed_time, log_result
from snips_nlu.common.utils import (
    check_persisted_path,
    deduplicate_overlapping_entities,
    fitted_required,
    json_string,
    replace_entities_with_placeholders,
)
from snips_nlu.constants import (
    DATA,
    ENTITIES,
    ENTITY,
    INTENTS,
    LANGUAGE,
    RES_INPUT,
    RES_INTENT,
    RES_INTENT_NAME,
    RES_SLOTS,
    SLOT_NAME,
    TEXT,
    UTTERANCES,
)
from snips_nlu.dataset import validate_and_format_dataset
from snips_nlu.exceptions import IntentNotFoundError, LoadingError
from snips_nlu.intent_parser.intent_parser import IntentParser
from snips_nlu.pipeline.configs import LookupIntentParserConfig
from snips_nlu.preprocessing import normalize_token, tokenize, tokenize_light
from snips_nlu.resources import get_stop_words
from snips_nlu.result import (
    empty_result,
    intent_classification_result,
    parsing_result,
    unresolved_slot,
)

logger = logging.getLogger(__name__)


@IntentParser.register("lookup_intent_parser")
class LookupIntentParser(IntentParser):
    """Intent parser using pattern matching in a deterministic manner

    This intent parser is very strict by nature, and tends to have a very good
    precision but a low recall. For this reason, it is interesting to use it
    first before potentially falling back to another parser.
    """

    config_type = LookupIntentParserConfig

    def __init__(self, config=None, **shared):
        """The deterministic intent parser can be configured by passing a
        :class:`.DeterministicIntentParserConfig`"""
        super(LookupIntentParser, self).__init__(config, **shared)
        self._language = None
        self.stop_words = None
        self.map = None
        self.intents_names = []
        self.slots_names = []
        self._intents_mapping = {}
        self._slots_mapping = {}

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, value):
        self._language = value
        if value is None:
            self.stop_words = None
        else:
            if self.config.ignore_stop_words:
                self.stop_words = get_stop_words(self.resources)
            else:
                self.stop_words = set()

    @property
    def fitted(self):
        """Whether or not the intent parser has already been trained"""
        return self.map is not None

    @log_elapsed_time(
        logger, logging.INFO,
        "Fitted dict deterministic parser in {elapsed_time}"
    )
    def fit(self, dataset, force_retrain=True):
        """Fits the intent parser with a valid Snips dataset"""
        logger.info("Fitting dict deterministic parser...")
        dataset = validate_and_format_dataset(dataset)
        self.load_resources_if_needed(dataset[LANGUAGE])
        self.fit_builtin_entity_parser_if_needed(dataset)
        self.fit_custom_entity_parser_if_needed(dataset)
        self.language = dataset[LANGUAGE]
        self.map = dict()
        entity_placeholders = _get_entity_placeholders(dataset, self.language)

        for (key, val) in self.generate_io_mapping(
                dataset[INTENTS], entity_placeholders
        ):
            # handle key collisions -*- remove ambiguous entries -*-
            if key in self.map and self.map[key] != val:
                self.map.pop(key)
            else:
                self.map[key] = val

        return self

    @log_result(
        logger, logging.DEBUG,
        "DictDeterministicIntentParser result -> {result}"
    )
    @log_elapsed_time(logger, logging.DEBUG, "Parsed in {elapsed_time}.")
    @fitted_required
    def parse(self, text, intents=None, top_n=None):
        """Performs intent parsing on the provided *text*

        Intent and slots are extracted simultaneously through pattern matching

        Args:
            text (str): input
            intents (str or list of str): if provided, reduces the scope of
                intent parsing to the provided list of intents
            top_n (int, optional): when provided, this method will return a
                list of at most top_n most likely intents, instead of a single
                parsing result.
                Note that the returned list can contain less than ``top_n``
                elements, for instance when the parameter ``intents`` is not
                None, or when ``top_n`` is greater than the total number of
                intents.

        Returns:
            dict or list: the most likely intent(s) along with the extracted
            slots. See :func:`.parsing_result` and :func:`.extraction_result`
            for the output format.

        Raises:
            NotTrained: when the intent parser is not fitted
        """
        if isinstance(intents, str):
            intents = [intents]

        builtin_entities = self.builtin_entity_parser.parse(text,
                                                            use_cache=True)
        custom_entities = self.custom_entity_parser.parse(text, use_cache=True)
        all_entities = builtin_entities + custom_entities

        all_entities = deduplicate_overlapping_entities(all_entities)

        placeholder_fn = lambda entity_name: _get_entity_name_placeholder(
            entity_name, self.language
        )
        _, processed_text = replace_entities_with_placeholders(
            text, all_entities, placeholder_fn
        )

        cleaned_processed_text = self._preprocess_text(processed_text)
        cleaned_text = self._preprocess_text(text).lower()
        cleaned_text = "".join(
            [t for t in tokenize_light(cleaned_text, self.language)])

        key = "".join(
            [
                t
                for t in tokenize_light(cleaned_processed_text, self.language)
                if normalize(t) not in self.stop_words
            ]
        ).lower()
        val = self.map.get(key)

        if val is None:
            val = self.map.get(cleaned_text)
            all_entities = []

        # conform to api
        result = self.parse_map_output(text, val, all_entities, intents)
        if top_n is not None:
            # convert parsing_result to extraction_result and return a list
            result.pop(RES_INPUT)
            result = [result]

        return result

    def parse_map_output(self, text, output, entities, intents):
        """Parse the trie output to the parser's result format"""
        if not output:
            return empty_result(text, 1.0)
        intent_id, slot_ids = output
        intent_name = self.intents_names[intent_id]
        if intents is not None and intent_name not in intents:
            return empty_result(text, 1.0)

        entities.sort(key=lambda a: a["range"]["start"])
        parsed_intent = intent_classification_result(
            intent_name=intent_name, probability=1.0
        )
        slots = []
        # assert invariant
        assert len(slot_ids) == len(entities)
        for slot_id, entity in zip(slot_ids, entities):
            slot_name = self.slots_names[slot_id]
            slot_value = text[entity["range"]["start"]: entity["range"]["end"]]
            entity_name = entity["entity_kind"]
            start_end = [entity["range"]["start"], entity["range"]["end"]]
            slot = unresolved_slot(start_end, slot_value, entity_name,
                                   slot_name)
            slots.append(slot)

        return parsing_result(text, parsed_intent, slots)

    @fitted_required
    def get_intents(self, text):
        """Returns the list of intents ordered by decreasing probability

        The length of the returned list is exactly the number of intents in the
        dataset + 1 for the None intent
        """
        intents = []
        res = self.parse(text)
        try:
            matched_intent = res[RES_INTENT]
            intent_name = matched_intent[RES_INTENT_NAME]
            intents.append(matched_intent)
            others = [x for x in self.intents_names if x != intent_name]
        except TypeError:
            others = [x for x in self.intents_names]

        for intent in others:
            intents.append(intent_classification_result(intent, 0.0))

        intents.append(intent_classification_result(None, 0.0))

        return intents

    @fitted_required
    def get_slots(self, text, intent):
        """Extracts slots from a text input, with the knowledge of the intent

        Args:
            text (str): input
            intent (str): the intent which the input corresponds to

        Returns:
            list: the list of extracted slots

        Raises:
            IntentNotFoundError: When the intent was not part of the training
                data
        """
        if intent is None:
            return []

        if intent not in self.intents_names:
            raise IntentNotFoundError(intent)

        slots = self.parse(text, intents=[intent])[RES_SLOTS]
        if slots is None:
            slots = []
        return slots

    def get_intent_id(self, intent_name):
        intent_id = self._intents_mapping.get(intent_name)
        if intent_id is None:
            intent_id = len(self.intents_names)
            self.intents_names.append(intent_name)
            self._slots_mapping[intent_name] = intent_id

        return intent_id

    def get_slot_id(self, slot_name):
        slot_id = self._slots_mapping.get(slot_name)
        if slot_id is None:
            slot_id = len(self.slots_names)
            self.slots_names.append(slot_name)
            self._slots_mapping[slot_name] = slot_id

        return slot_id

    def _preprocess_text(self, txt):
        """Replaces stop words and characters that are tokenized out by
            whitespaces"""
        tokens = tokenize(txt, self.language)
        current_idx = 0
        cleaned_string = ""
        for token in tokens:
            if self.stop_words and normalize_token(token) in self.stop_words:
                token.value = "".join(" " for _ in range(len(token.value)))
            prefix_length = token.start - current_idx
            cleaned_string += "".join((" " for _ in range(prefix_length)))
            cleaned_string += token.value
            current_idx = token.end
        suffix_length = len(txt) - current_idx
        cleaned_string += "".join((" " for _ in range(suffix_length)))
        return cleaned_string

    def generate_io_mapping(self, intents, entity_placeholders):
        """Generate input-output pairs"""
        for intent_name, intent in iteritems(intents):
            intent_id = self.get_intent_id(intent_name)
            for entry in intent[UTTERANCES]:
                yield self._build_io_mapping(intent_id, entry,
                                             entity_placeholders)

    def _build_io_mapping(self, intent_id, utterance, entity_placeholders):
        input_ = []
        output = [intent_id]
        slots = []
        for chunk in utterance[DATA]:
            if SLOT_NAME in chunk:
                slot_name = chunk[SLOT_NAME]
                slot_id = self.get_slot_id(slot_name)
                entity_name = chunk[ENTITY]
                placeholder = entity_placeholders[entity_name]
                input_.append(placeholder.lower())
                slots.append(slot_id)
            else:
                input_.append(chunk[TEXT])
        output.append(slots)

        key = "".join(
            [
                t
                for t in tokenize_light(" ".join(input_), self.language)
                if normalize(t) not in self.stop_words
            ]
        )

        return key.lower(), output

    @check_persisted_path
    def persist(self, path):
        """Persists the object at the given path"""
        path.mkdir()
        parser_json = json_string(self.to_dict())
        parser_path = path / "intent_parser.json"

        with parser_path.open(mode="w") as f:
            f.write(parser_json)
        self.persist_metadata(path)

    @classmethod
    def from_path(cls, path, **shared):
        """Loads a :class:`DeterministicIntentParser` instance from a path

        The data at the given path must have been generated using
        :func:`~DeterministicIntentParser.persist`
        """
        path = Path(path)
        model_path = path / "intent_parser.json"
        if not model_path.exists():
            raise LoadingError(
                "Missing deterministic intent parser metadata file: %s"
                % model_path.name
            )

        with model_path.open(encoding="utf8") as f:
            metadata = json.load(f)
        return cls.from_dict(metadata, **shared)

    def to_dict(self):
        """Returns a json-serializable dict"""
        return {
            "config": self.config.to_dict(),
            "language_code": self.language,
            "map": self.map,
            "slots_names": self.slots_names,
            "intents_names": self.intents_names,
        }

    @classmethod
    def from_dict(cls, unit_dict, **shared):
        """Creates a :class:`DeterministicIntentParser` instance from a dict

        The dict must have been generated with
        :func:`~DeterministicIntentParser.to_dict`
        """
        config = cls.config_type.from_dict(unit_dict["config"])
        parser = cls(config=config, **shared)
        parser.language = unit_dict["language_code"]
        parser.map = unit_dict["map"]
        parser.slots_names = unit_dict["slots_names"]
        parser.intents_names = unit_dict["intents_names"]

        return parser


def _get_entity_placeholders(dataset, language):
    return {e: _get_entity_name_placeholder(e, language) for e in
            dataset[ENTITIES]}


def _get_entity_name_placeholder(entity_label, language):
    return "%%%s%%" % "".join(tokenize_light(entity_label, language)).upper()