# Copyright (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""The mind map is decoded against a jsMind grammar and saved only as valid JSON."""

import json
import os
import sys
from unittest.mock import patch

import pytest

_SC_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)

from components import mindmap_component as mc
from components.mindmap_component import (
    MAX_DEPTH,
    MindmapComponent,
    mindmap_json_schema,
    parse_mindmap,
)

VALID = {
    "meta": {"name": "影子", "author": "AI Assistant", "version": "1.0"},
    "format": "node_tree",
    "data": {"id": "root", "topic": "影子的秘密", "children": [
        {"id": "条件", "topic": "影子产生的必要条件", "children": [
            {"id": "光源", "topic": "必须有光源照射"},
        ]},
    ]},
}

# A real reply (session 20261021-134011-2ebd): the opening quote of a topic
# value is missing, so json.loads and the UI both reject the whole map.
DROPPED_QUOTE = """{
  "meta": {"name": "影子", "author": "AI Assistant", "version": "1.0"},
  "format": "node_tree",
  "data": {
    "id": "root",
    "topic": "影子的秘密",
    "children": [
      {
        "id": "演示验证",
        "topic":通过学生上台演示验证光影关系"
      }
    ]
  }
}"""


class FakeModel:
    def __init__(self, reply, raise_on_schema=False):
        self.reply = reply
        self.raise_on_schema = raise_on_schema
        self.calls = []

    def generate(self, *, messages, stream=False, **kwargs):
        if self.raise_on_schema and "json_schema" in kwargs:
            raise TypeError("generate() got an unexpected keyword 'json_schema'")
        self.calls.append(kwargs)
        return self.reply


def _component(model):
    component = MindmapComponent("s1", "vlm", "m", "GPU")
    component.model = model
    return component


def _run(component, tmp_path):
    path = tmp_path / "mindmap.mmd"
    with patch.object(mc.SessionPaths, "mindmap_path", return_value=path), \
            patch.object(mc, "load_prompt", return_value="system"):
        returned = component.generate_mindmap("summary")
    return returned, path.read_text(encoding="utf-8")


def _depth(schema):
    children = schema["properties"].get("children")
    return 0 if children is None else 1 + _depth(children["items"])


def test_schema_pins_the_jsmind_envelope():
    schema = json.loads(mindmap_json_schema())
    assert schema["required"] == ["meta", "format", "data"]
    assert schema["properties"]["format"] == {"const": "node_tree"}
    assert schema["properties"]["data"]["properties"]["id"] == {"const": "root"}


def test_schema_nests_children_to_the_depth_bound():
    root = json.loads(mindmap_json_schema())["properties"]["data"]
    assert _depth(root) == MAX_DEPTH
    child = root["properties"]["children"]["items"]
    assert child["required"] == ["id", "topic"]
    assert child["additionalProperties"] is False


def test_generation_is_schema_constrained(tmp_path):
    model = FakeModel(json.dumps(VALID, ensure_ascii=False))
    _run(_component(model), tmp_path)
    assert model.calls[0]["json_schema"] == mindmap_json_schema()


def test_valid_reply_is_saved_as_valid_json(tmp_path):
    returned, saved = _run(_component(FakeModel(json.dumps(VALID, ensure_ascii=False))), tmp_path)
    assert json.loads(saved) == VALID
    assert returned == saved


def test_dropped_quote_is_repaired_before_saving(tmp_path):
    _, saved = _run(_component(FakeModel(DROPPED_QUOTE)), tmp_path)
    node = json.loads(saved)["data"]["children"][0]
    assert node == {"id": "演示验证", "topic": "通过学生上台演示验证光影关系"}


def test_a_backend_without_json_schema_is_retried_unconstrained(tmp_path):
    model = FakeModel(json.dumps(VALID, ensure_ascii=False), raise_on_schema=True)
    _, saved = _run(_component(model), tmp_path)
    assert model.calls == [{"enable_thinking": False, "finish_info": {}}]
    assert json.loads(saved) == VALID


@pytest.mark.parametrize("reply", ["", "not a mind map", '{"format": "node_tree"}',
                                   '{"meta": {}, "format": "outline", "data": {}}'])
def test_unrecoverable_reply_fails_instead_of_saving(reply, tmp_path):
    with pytest.raises(ValueError, match="INVALID_MINDMAP_FORMAT"):
        _run(_component(FakeModel(reply)), tmp_path)
    assert not (tmp_path / "mindmap.mmd").exists()


def test_parse_mindmap_tolerates_surrounding_whitespace():
    assert parse_mindmap("\n" + json.dumps(VALID) + "\n") == VALID
