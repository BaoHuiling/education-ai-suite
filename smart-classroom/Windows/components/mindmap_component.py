from components.base_component import PipelineComponent
from utils.runtime_config_loader import RuntimeConfig
from utils.config_loader import config
from utils.prompt_loader import load_prompt
from utils.storage_manager import StorageManager
from utils.session_paths import SessionPaths
import json
import json_repair
import logging, os

logger = logging.getLogger(__name__)

# Levels below the root the grammar allows. The prompt asks for 2-4; the grammar
# is unrolled to this depth rather than recursive, so the bound is exact.
MAX_DEPTH = 4


def _node_schema(depth: int) -> dict:
    """One jsMind node: id and topic, children only above the depth bound."""
    properties = {
        "id": {"type": "string", "minLength": 1},
        "topic": {"type": "string", "minLength": 1},
    }
    if depth < MAX_DEPTH:
        properties["children"] = {"type": "array", "items": _node_schema(depth + 1)}
    return {
        "type": "object",
        "properties": properties,
        "required": ["id", "topic"],
        "additionalProperties": False,
    }


def mindmap_json_schema() -> str:
    """Schema for the jsMind node_tree, passed to the decoder as a grammar.

    The model's free-form JSON is valid almost always, not always: a session
    once lost the opening quote of a topic value and the UI could not render
    the map at all. Constraining decoding rules that out.
    """
    root = _node_schema(0)
    root["properties"]["id"] = {"const": "root"}
    meta = {
        "type": "object",
        "properties": {key: {"type": "string"} for key in ("name", "author", "version")},
        "required": ["name", "author", "version"],
        "additionalProperties": False,
    }
    return json.dumps({
        "type": "object",
        "properties": {
            "meta": meta,
            "format": {"const": "node_tree"},
            "data": root,
        },
        "required": ["meta", "format", "data"],
        "additionalProperties": False,
    })


def _is_node(node) -> bool:
    if not isinstance(node, dict):
        return False
    if not isinstance(node.get("id"), str) or not isinstance(node.get("topic"), str):
        return False
    children = node.get("children", [])
    return isinstance(children, list) and all(_is_node(child) for child in children)


def _has_envelope(obj) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("meta"), dict)
        and obj.get("format") == "node_tree"
    )


def _is_mindmap(obj) -> bool:
    return _has_envelope(obj) and _is_node(obj.get("data"))


def _prune(node) -> tuple:
    """``node`` without the descendants that lack an id or topic, and how many
    were dropped. ``None`` when ``node`` itself is incomplete.

    A reply cut off mid-node repairs into a node with no topic; dropping it
    keeps everything written before the cut.
    """
    if not isinstance(node, dict):
        return None, 1
    if not isinstance(node.get("id"), str) or not isinstance(node.get("topic"), str):
        return None, 1
    kept_node = dict(node)
    children = node.get("children")
    kept_node.pop("children", None)
    dropped = 0
    if isinstance(children, list):
        kept = []
        for child in children:
            pruned, lost = _prune(child)
            dropped += lost
            if pruned is not None:
                kept.append(pruned)
        if kept:
            kept_node["children"] = kept
    return kept_node, dropped


def _recover(raw: str) -> tuple:
    """The node_tree in ``raw`` and the number of incomplete nodes dropped."""
    text = (raw or "").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if _is_mindmap(obj):
        return obj, 0
    repaired = json_repair.loads(text)
    if _is_mindmap(repaired):
        logger.warning("Mindmap output was not a valid node_tree; saved the repaired JSON.")
        return repaired, 0
    if _has_envelope(repaired):
        data, dropped = _prune(repaired.get("data"))
        if data is not None:
            repaired["data"] = data
            logger.warning(
                "Mindmap output was not a valid node_tree; dropped %d incomplete "
                "node(s) and saved the rest.", dropped,
            )
            return repaired, dropped
    raise ValueError(f"INVALID_MINDMAP_FORMAT: {text[:200]!r}")


def parse_mindmap(raw: str) -> dict:
    """The jsMind node_tree in ``raw``, repaired if the JSON is broken.

    Nodes cut off before their id or topic are dropped, so a reply that hit the
    token limit still saves what it wrote. Raises ``ValueError`` when not even
    the root node can be recovered, so a mind map the UI cannot render fails the
    stage instead of being saved as done.
    """
    return _recover(raw)[0]


class MindmapComponent(PipelineComponent):
    def __init__(self, session_id, provider, model_name, device, temperature=0.7):
        self.session_id = session_id
        self.provider = provider.lower()
        self.model_name = model_name
        self.device = device
        self.temperature = temperature

    def _get_mindmap_message(self, input_text):
        system_prompt = load_prompt("mindmap", config.app.language, "system_prompt")
        logger.debug("Mindmap system prompt loaded for lang=%s", config.app.language)
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{input_text}"}
        ]

    def _generate(self, messages: list, finish_info: dict) -> str:
        try:
            return self.model.generate(
                messages=messages,
                stream=False,
                enable_thinking=False,
                json_schema=mindmap_json_schema(),
                finish_info=finish_info,
            )
        except TypeError:
            logger.info("Backend does not accept json_schema; generating unconstrained.")
        except Exception as exc:
            logger.warning(
                "Constrained generation failed (%s); retrying unconstrained.", exc
            )
        finish_info.clear()
        return self.model.generate(messages=messages, stream=False, enable_thinking=False,
                                   finish_info=finish_info)

    def _warn_if_truncated(self, finish_info: dict, raw: str) -> bool:
        if finish_info.get("truncated") is not True:
            return False
        logger.warning(
            "MINDMAP_TRUNCATED: session %s mind map output stopped at the token limit "
            "(%s of max_new_tokens=%s); the mind map will be incomplete. Raise "
            "models.text_gen.max_new_tokens in config.yaml to avoid this. Output tail: %r",
            self.session_id,
            finish_info.get("output_tokens"),
            finish_info.get("max_new_tokens"),
            (raw or "")[-300:],
        )
        return True

    def generate_mindmap(self, summary_text):
        mindmap_path = str(SessionPaths.mindmap_path(self.session_id))

        try:
            logger.info("Generating mindmap from summary...")
            finish_info: dict = {}
            raw = self._generate(self._get_mindmap_message(summary_text), finish_info)
            truncated = self._warn_if_truncated(finish_info, raw)
            mindmap, dropped = _recover(raw)
            if truncated:
                logger.warning(
                    "MINDMAP_TRUNCATED: session %s saved the truncated mind map "
                    "(%d incomplete node(s) dropped).", self.session_id, dropped,
                )
            full_mindmap = json.dumps(mindmap, ensure_ascii=False, indent=2)
            StorageManager.save(mindmap_path, full_mindmap, append=False)
            logger.info("Mindmap generation completed successfully.")
            return full_mindmap

        except Exception as e:
            logger.error(f"Mindmap generation failed: {e}")
            raise e
