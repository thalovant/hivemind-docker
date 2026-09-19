"""Make a malformed intent context fail as ``MalformedSession``.

``Session.deserialize`` documents, and ``SessionManager`` relies on, one
failure mode for a bad carrier: ``MalformedSession``. The ``ovos.session.sync``
handler catches exactly that, logs it, and carries on with ``inbound = None``.

The intent-context parser does not honour that contract. Against 2.11.18a1 a
peer sending ``{"context": 5}``, ``{"context": {"frame_stack": "x"}}`` or a
frame whose entities are not a mapping raises ``AttributeError``/``TypeError``/
``ValueError`` straight out of the parser. Those sail past the one handler that
exists and surface on the bus client's receive thread, so a single peer's bad
frame takes the transport down instead of dropping the one message.

Carried from thalovant/hivemind-docker#8, which fixed it in a vendored copy of
the client. The vendored copy is gone; this patches the installed one so the
fix does not depend on re-forking. Retire it once the guard is in a release.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path


PACKAGE = "ovos_bus_client.session"

# Anchor on the INSERTION POINT -- the one call that must be guarded -- not on
# the surrounding block. A patch anchored on a whole body breaks the moment
# upstream adds an unrelated statement next to it, and then silently protects
# nothing.
#
# Matched as a WHOLE LINE, never as a substring. The anchor carries its own
# indentation, and a substring test would also match the same call indented
# more deeply -- splicing the guard into the middle of that deeper indentation
# and leaving a file that no longer parses.
ANCHOR_LINE = '        context = IntentContextManager.deserialize(data.get("context", {}))'

GUARDED = '''        try:
            raw_context = data.get("context", {})
            _validate_legacy_context_shape(raw_context)
            context = IntentContextManager.deserialize(raw_context)
        except (AttributeError, TypeError, ValueError) as error:
            # The carrier's own contract: a malformed session is rejected as
            # MalformedSession, which SessionManager already catches. Letting
            # the parser's raw error escape kills the receive thread instead.
            raise MalformedSession(
                f"session carries a malformed intent context: {error}"
            ) from error
'''

# Lifted verbatim from the upstream change so the two cannot drift. The parser
# alone is not enough: it accepts a frame whose entities is a string, and the
# shape is not exercised until Session.__init__ folds the stack, which is
# outside the guard -- so a payload the guard accepted still put an
# AttributeError on the reader thread.
HELPER = '''def _validate_legacy_context_shape(raw) -> None:
    """Reject a legacy ``context`` the session fold cannot consume.

    ``IntentContextManager.deserialize`` is lenient: it will happily build a
    frame whose ``entities`` is a string, because it only unpacks pairs and
    passes the frame dict through. The shape is not actually exercised until
    ``Session.__init__`` folds the stack into ``intent_context``, and that fold
    is outside every handler written for ``deserialize`` -- so a peer could put
    an ``AttributeError`` on the reader's thread with a payload this function
    had already accepted.

    The accepted shape is exactly what ``IntentContextManager.serialize``
    emits: ``frame_stack`` a list of ``(frame, timestamp)`` pairs, each frame a
    mapping, its ``entities`` a list of mappings (``_entity_to_entry`` calls
    ``.get`` on each). Tuples are accepted alongside lists because an in-process
    round trip never passes through JSON.
    """
    if not isinstance(raw, dict):
        raise TypeError(f"context must be a mapping, got {type(raw).__name__}")
    frames = raw.get("frame_stack", [])
    if not isinstance(frames, (list, tuple)):
        raise TypeError(
            f"frame_stack must be a list, got {type(frames).__name__}")
    for frame in frames:
        if not (isinstance(frame, (list, tuple)) and len(frame) == 2):
            raise ValueError(
                "each frame_stack item must be a (frame, timestamp) pair")
        payload = frame[0]
        if not isinstance(payload, dict):
            raise TypeError(
                f"frame must be a mapping, got {type(payload).__name__}")
        entities = payload.get("entities", [])
        if not isinstance(entities, (list, tuple)):
            raise TypeError(
                f"frame entities must be a list, got {type(entities).__name__}")
        for entity in entities:
            if not isinstance(entity, dict):
                raise TypeError(
                    f"each entity must be a mapping, got {type(entity).__name__}")
'''

CLASS_ANCHOR_LINE = "class Session(_SpecSession):"


def main() -> None:
    spec = importlib.util.find_spec(PACKAGE)
    if spec is None or spec.origin is None:
        raise SystemExit(f"{PACKAGE} is not installed")

    path = Path(spec.origin)
    source = path.read_text()

    if GUARDED in source:
        return

    lines = source.splitlines(keepends=True)

    def sole(anchor: str, what: str) -> int:
        found = [i for i, line in enumerate(lines) if line.rstrip("\n") == anchor]
        if len(found) != 1:
            raise SystemExit(
                f"expected exactly one {what} anchor in {path}, found "
                f"{len(found)}. Upstream moved it: check whether the guard is "
                "now applied upstream and drop this patch, or re-anchor it."
            )
        return found[0]

    call = sole(ANCHOR_LINE, "intent-context deserialize")
    cls = sole(CLASS_ANCHOR_LINE, "Session class")

    # Insert from the BOTTOM up so the first edit cannot shift the second
    # index. The helper goes above the class that uses it.
    lines[call] = GUARDED
    lines[cls] = HELPER + "\n\n" + lines[cls]
    path.write_text("".join(lines))


def verify() -> None:
    """Prove the guard holds, here, rather than discovering it in production.

    This runs in a FRESH interpreter on purpose. ``find_spec`` above imports the
    parent package, whose ``__init__`` already pulls in this submodule, so an
    in-process import would hand back the pre-patch module from ``sys.modules``
    and report a failure the patch had in fact fixed.
    """
    check = textwrap.dedent(
        """
        from ovos_bus_client.session import MalformedSession, Session

        for payload in (
            {"context": 5},
            {"context": {"frame_stack": "not-a-list"}},
            {"context": {"frame_stack": [["x", 1]]}},
            {"context": {"frame_stack": [{"entities": "nope"}]}},
            # these three only detonate when Session.__init__ folds the stack,
            # which is why guarding the parse alone was not enough
            {"context": {"frame_stack": [[{"entities": "nope"}, 1]]}},
            {"context": {"frame_stack": ""}},
            {"context": {"frame_stack": {}}},
        ):
            try:
                Session.deserialize(payload)
            except MalformedSession:
                continue
            except Exception as error:
                raise SystemExit(
                    f"a malformed intent context still escapes as "
                    f"{type(error).__name__}: {payload!r}"
                )
            raise SystemExit(f"a malformed intent context did not raise: {payload!r}")

        # A well-formed carrier must be untouched by the guard, including
        # whatever the library's own serializer emits -- the shape check is
        # written against that output, so this is the regression that matters.
        import json
        from ovos_bus_client.session import IntentContextManager

        assert Session.deserialize({"session_id": "ok", "context": {}}).session_id == "ok"
        assert Session.deserialize({"session_id": "bare"}).session_id == "bare"
        manager = IntentContextManager()
        manager.inject_context({"data": [["value", "key"]], "key": "key",
                                "confidence": 1.0})
        round_trip = {"session_id": "rt", "context": manager.serialize()}
        assert Session.deserialize(round_trip).session_id == "rt"
        assert Session.deserialize(
            json.loads(json.dumps(round_trip))).session_id == "rt"
        """
    )
    result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "the MalformedSession guard did not take:\n"
            + (result.stderr or result.stdout).strip()
        )


if __name__ == "__main__":
    main()
    verify()
