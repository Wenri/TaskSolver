"""
The atomic unit of a memory is an event.
A collection of events form an EventCollection.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast
from wirecap.types import JSONObject, json_load_object
from .common import Question, ParsedAnswer
from bson import ObjectId
import datetime

# TODO consider how to embed images in json files too.
def file_location_type(path:str) -> str:
    """ given a type, it returns a string indicating whether this is `local`, or `url`
    """
    if path.startswith('http://') or path.startswith('https://'):
        return 'url'
    return 'local'

def read_event_file(file_location: str) -> JSONObject:
    """ Event files loaded as json
    """
    with open(file_location, 'r') as f:
        values = json_load_object(f)
    return values

def write_event_file(file_location: str, values: dict[str, object]) -> str:
    """ Event files stored as json.
    """
    if file_location_type(file_location) == 'local':
        Path(file_location).parent.mkdir(parents=True, exist_ok=True)
        with open(file_location, 'w') as f:
            json.dump(values,f)
    else:
        raise NotImplementedError(f'Uncaught file location type {file_location_type(file_location)}')
    return file_location


class EventCollection(object):
    """ Class used for data processing from event files
    """
    def __init__(self) -> None:
        self.id = str(ObjectId())
        self.events: list[Event] = []

    def load_from_session(self, event_dir:str, session_token:str) -> "EventCollection":
        if file_location_type (event_dir) == 'local':
            assert os.path.exists(os.path.join(event_dir, session_token)), f'{os.path.join(event_dir, session_token)} does not exist'

            for event_file in os.listdir(os.path.join(event_dir, session_token)):
                # try:
                new_event = Event().load_from_event_file(os.path.join(event_dir, session_token, event_file))
                # except:
                #     logger.info(f'Error loading from {os.path.join(event_dir, session_token, event_file)}')
                #     continue
                self.add_event(new_event)
        else:
            raise NotImplementedError(f'Uncaught file location {file_location_type(event_dir)}')

        self.time_sorted()
        return self

    def add_event(self, event:"Event") -> "EventCollection":
        self.events.append(event)
        return self

    def time_sorted(self) -> "EventCollection":
        self.events = sorted(self.events)
        return self

    def __str__(self) -> str:
        printout = ""
        for ev in sorted(self.events):
            printout += str(ev)+'\n'
        return printout

    def  __len__(self) -> int:
        return len(self.events)

    def filter_to(self, label: str | list[str]) -> list[Event]:
        if isinstance(label, str):
            label = [label]

        permissible_classes: list[type[Event]] = []
        for l in label:
            assert l in TYPE2CLASS, f"unknown type {label}"
            permissible_classes.append(TYPE2CLASS[l])

        filtered_events = [ev for ev in self.events if isinstance(ev, tuple(permissible_classes))]
        return filtered_events


class Event(object):
    def __init__(self, session_token: str | None = None) -> None:
        self.session_token = session_token
        self.timestamp = str(datetime.datetime.now())
        self.type = 'EVENT'

    # comparison methods for sorting
    def __lt__(self, obj: object) -> bool:
        if not isinstance(obj, Event): raise ValueError(f'Invalid comparison of type {type(obj)}')
        return self.timestamp < obj.timestamp
    def __gt__(self, obj: object) -> bool:
        if not isinstance(obj, Event): raise ValueError(f'Invalid comparison of type {type(obj)}')
        return self.timestamp > obj.timestamp
    def __le__(self, obj: object) -> bool:
        if not isinstance(obj, Event): raise ValueError(f'Invalid comparison of type {type(obj)}')
        return self.timestamp <= obj.timestamp
    def __ge__(self, obj: object) -> bool:
        if not isinstance(obj, Event): raise ValueError(f'Invalid comparison of type {type(obj)}')
        return self.timestamp >= obj.timestamp
    def __eq__(self, obj: object) -> bool:
        if not isinstance(obj, Event): raise ValueError(f'Invalid comparison of type {type(obj)}')
        return self.timestamp == obj.timestamp

    def load_from_event_params(self, **kwargs: object) -> "Event":
        for key in kwargs:
            setattr(self, key, kwargs[key])
        return self

    def load_from_event_file(self, filepath: str) -> "Event":
        fields = read_event_file(filepath)
        event_type = fields['type']
        if not isinstance(event_type, str):
            raise TypeError('event type must be a string')
        return TYPE2CLASS[event_type]().load_from_event_params(**fields)

    def export(self) -> dict[str, object]:
        return cast(dict[str, object], vars(self))

    def save_to_event_file(self, filepath: str) -> str:
        attr_dict = self.export()
        if getattr(self, 'latency', False) is None:
            raise ValueError("Forgot to call .tick and .tock?")
        write_event_file(filepath, attr_dict)
        return filepath

    @property
    def description(self) -> str:
        raise NotImplementedError

    def __str__(self) -> str:
        return f'{str(self.timestamp)}: [{self.type}]\n{self.description}'


class ThinkEvent(Event):
    def __init__(self, session_token: str | None = None, qa_sequence: list[tuple[Question, ParsedAnswer]] | None = None) -> None:
        super().__init__(session_token)
        assert qa_sequence is not None, "qa_sequence must be a list, even if length 1."

        self.qa_sequence = qa_sequence

    @property
    def description(self) -> str:
        st = ""
        for idx, qa in enumerate(self.qa_sequence):
            q, a = qa
            st += f"#### Question {idx}\n"
            st += str(q)
            st += "\n"
            st += f"#### Answer{idx}\n"
            st += str(a)
            st += "\n"
        return st

class ActEvent(Event):
    def __init__(self, session_token: str | None = None) -> None:
        super().__init__(session_token)

    @property
    def description(self) -> str:
        raise NotImplementedError

class ActErrorEvent(Event):
    """ Event of an error in the execution of an action.
    """
    def __init__(self, session_token: str | None = None,
                 exception: Exception | None = None) -> None:
        super().__init__(session_token)
        self.exception = exception

    @property
    def description(self) -> str:
        raise NotImplementedError


class ObserveEvent(Event):
    def __init__(self, session_token: str | None = None) -> None:
        super().__init__(session_token)

    @property
    def description(self) -> str:
        raise NotImplementedError

class EvaluateEvent(Event):
    def __init__(self, session_token: str | None = None,
                 completion_question: Question | None = None,
                 completion_eval: ParsedAnswer | None = None) -> None:
        super().__init__(session_token)
        assert isinstance(completion_question, Question)
        assert isinstance(completion_eval, ParsedAnswer)
        self.completion_question = completion_question
        self.completion_eval = completion_eval

    @property
    def description(self) -> str:
        st = "## Evaluation Question\n"
        st += "\n"
        st += str(self.completion_question)
        st += "\n"
        st += "## Evaluation Answer\n"
        st += "\n"
        st += str(self.completion_eval)
        st += "\n"
        return st

class FeedbackEvent(Event):
    def __init__(self, session_token: str | None = None,
                 feedback: Question | None = None) -> None:
        super().__init__(session_token)
        self.feedback = feedback

    @property
    def description(self)  -> str:
        st = "## Reflection:\n"
        st += str(self.feedback)
        st += "\n"
        return st


class InteractEvent(Event):
    def __init__(self, session_token: str | None = None) -> None:
        super().__init__(session_token)

    @property
    def description(self) -> str:
        raise NotImplementedError


TYPE2CLASS: dict[str, type[Event]] = {
'EVENT': Event,
# TAORI events
'THINK': ThinkEvent,
'ACT': ActEvent,
'ACTERROR': ActErrorEvent,
'OBSERVE': ObserveEvent,
'EVALUATE': EvaluateEvent,
'FEEDBACK': FeedbackEvent,
'INTERACT': InteractEvent,
}
