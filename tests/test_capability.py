import copy
import json
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai import exceptions as ai_exc
from pydantic_ai import messages as ai_messages
from pydantic_ai import usage as ai_usage
from pydantic_ai.models import function as ai_function

import soliplex_a2ui
from soliplex_a2ui import capability as a2ui_capability

SURFACE_ID = "surface_s1"
DATA = {"person": {"name": "Ada", "tags": ["x"]}, "note": None}
VALID_COMPONENTS = [
    {"id": "root", "component": "Card", "child": "col"},
    {"id": "col", "component": "Column", "children": ["title", "name"]},
    {"id": "title", "component": "Text", "text": "Person"},
    {"id": "name", "component": "Text", "text": {"@path": "/person/name"}},
]


def _components(mutate):
    components = copy.deepcopy(VALID_COMPONENTS)
    mutate(components)
    return components


def test_surface_operations():
    found = a2ui_capability.surface_operations(
        SURFACE_ID, VALID_COMPONENTS, DATA
    )

    assert found == [
        {
            "version": "1.0",
            "createSurface": {
                "surfaceId": SURFACE_ID,
                "catalogId": soliplex_a2ui.BASIC_CATALOG_ID,
            },
        },
        {
            "version": "1.0",
            "updateComponents": {
                "surfaceId": SURFACE_ID,
                "components": VALID_COMPONENTS,
            },
        },
        {
            "version": "1.0",
            "updateDataModel": {
                "surfaceId": SURFACE_ID,
                "path": "/",
                "value": DATA,
            },
        },
    ]


def test_validation_errors_accepts_valid_surface():
    operations = a2ui_capability.surface_operations(
        SURFACE_ID, VALID_COMPONENTS, DATA
    )

    assert a2ui_capability.validation_errors(operations) == []


def _unknown_component(cs):
    cs[2]["component"] = "Marquee"


def _missing_required(cs):
    del cs[2]["text"]


def _wrong_type(cs):
    cs[2]["text"] = 42


def _duplicate_id(cs):
    cs[3]["id"] = "title"


def _no_root(cs):
    cs[0]["id"] = "card"


def _dangling_child(cs):
    cs[1]["children"].append("missing")


def _cycle(cs):
    cs[1]["children"].append("root")


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (_unknown_component, "Marquee"),
        (_missing_required, "'text' is a required property"),
        (_wrong_type, "Expected string"),
        (_duplicate_id, "Duplicate component ID"),
        (_no_root, "Missing root component"),
        (_dangling_child, "Dangling reference"),
        (_cycle, "Circular reference"),
    ],
)
def test_validation_errors_rejects(mutate, expected):
    operations = a2ui_capability.surface_operations(
        SURFACE_ID, _components(mutate), DATA
    )

    (error,) = a2ui_capability.validation_errors(operations)

    assert expected in error


def _layout_call(info, components):
    (output_tool,) = info.output_tools
    return ai_messages.ModelResponse(
        parts=[
            ai_messages.ToolCallPart(
                tool_name=output_tool.name,
                args={"components": components},
            )
        ]
    )


def _room_model(generator, room_data=DATA):
    """One function model for both agents: the room agent calls
    'generate_a2ui' and then answers; the generating agent, the only one
    offered an output tool, is answered by 'generator'."""

    def respond(messages, info):
        if info.output_tools:
            return generator(messages, info)
        last = messages[-1].parts[-1]
        if isinstance(last, ai_messages.ToolReturnPart):
            return ai_messages.ModelResponse(
                parts=[ai_messages.TextPart(content="done")]
            )
        return ai_messages.ModelResponse(
            parts=[
                ai_messages.ToolCallPart(
                    tool_name="generate_a2ui",
                    args={"description": "a person card", "data": room_data},
                    tool_call_id="call1",
                )
            ]
        )

    return ai_function.FunctionModel(respond)


def _tool_return(result):
    (part,) = [
        part
        for message in result.all_messages()
        for part in message.parts
        if isinstance(part, ai_messages.ToolReturnPart)
        and part.tool_name == "generate_a2ui"
    ]
    return part


class _Generator:
    """Answer the generating agent's requests in order."""

    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    def __call__(self, messages, info):
        response = self.responses[len(self.calls)]
        self.calls.append(messages)
        return response(info)


def _valid(info):
    return _layout_call(info, VALID_COMPONENTS)


def _invalid(info):
    return _layout_call(info, _components(_no_root))


def _malformed(info):
    (output_tool,) = info.output_tools
    return ai_messages.ModelResponse(
        parts=[
            ai_messages.ToolCallPart(
                tool_name=output_tool.name,
                args={"components": "not a list"},
            )
        ]
    )


def _agent(generator, **kwargs):
    return Agent(
        _room_model(generator),
        capabilities=[a2ui_capability.A2UICapability(**kwargs)],
    )


async def test_generate_a2ui_paints_surface():
    generator = _Generator(_valid)
    room_data = copy.deepcopy(DATA)
    agent = Agent(
        _room_model(generator, room_data=room_data),
        capabilities=[a2ui_capability.A2UICapability()],
    )

    result = await agent.run("show Ada")

    part = _tool_return(result)
    exp_operations = a2ui_capability.surface_operations(
        "surface_call1", VALID_COMPONENTS, DATA
    )
    assert part.metadata == [
        soliplex_a2ui.surface_event("call1", exp_operations)
    ]
    assert "surface_call1" in part.content
    # The data model is the room agent's data, and the room agent's own
    # copy is left as it was.
    (event,) = part.metadata
    assert event.content["a2ui_operations"][2]["updateDataModel"]["value"] == (
        DATA
    )
    assert room_data == DATA
    (prompt,) = [
        p
        for p in generator.calls[0][0].parts
        if isinstance(p, ai_messages.UserPromptPart)
    ]
    assert "a person card" in prompt.content
    assert json.dumps(DATA) in prompt.content


async def test_generating_agent_instructions_carry_catalog():
    generator = _Generator(_valid)

    await _agent(generator).run("show Ada")

    request = generator.calls[0][0]
    assert soliplex_a2ui.BASIC_CATALOG_ID in request.instructions
    assert '"TextField"' in request.instructions


async def test_generate_a2ui_retries_invalid_layout():
    generator = _Generator(_invalid, _valid)

    result = await _agent(generator).run("show Ada")

    part = _tool_return(result)
    assert part.metadata is not None
    # The second attempt is told what the validator rejected.
    assert "Missing root component" in str(generator.calls[1][-1])
    # room: call + answer; generating agent: two attempts
    assert result.usage.requests == 4


async def test_generate_a2ui_reports_exhausted_validation():
    generator = _Generator(_invalid, _invalid)

    result = await _agent(generator, max_retries=1).run("show Ada")

    part = _tool_return(result)
    assert part.metadata is None
    assert part.content.startswith("Could not generate a valid surface")
    assert "Missing root component" in part.content


async def test_generate_a2ui_reports_exhausted_malformed_output():
    generator = _Generator(_malformed, _malformed)

    result = await _agent(generator, max_retries=1).run("show Ada")

    part = _tool_return(result)
    assert part.metadata is None
    assert part.content.startswith("Could not generate a valid surface")
    assert "components" in part.content


class InvalidProviderResponse(ai_exc.UnexpectedModelBehavior):
    def __init__(self):
        super().__init__("invalid provider response")


async def test_generate_a2ui_propagates_provider_failure():
    def broken(info):
        raise InvalidProviderResponse()

    with pytest.raises(
        ai_exc.UnexpectedModelBehavior, match="invalid provider response"
    ):
        await _agent(_Generator(broken)).run("show Ada")


async def test_generate_a2ui_respects_usage_limits():
    generator = _Generator(_invalid, _invalid, _invalid, _invalid)

    with pytest.raises(ai_exc.UsageLimitExceeded):
        await _agent(generator).run(
            "show Ada",
            usage_limits=ai_usage.UsageLimits(request_limit=2),
        )

    # The room agent's first request plus one generating request reach
    # the limit; the next generating request is refused.
    assert len(generator.calls) == 1


async def test_generate_a2ui_uses_configured_model():
    room_generator = _Generator()
    generator = _Generator(_valid)
    capability = a2ui_capability.A2UICapability(
        model=ai_function.FunctionModel(generator)
    )
    agent = Agent(_room_model(room_generator), capabilities=[capability])

    result = await agent.run("show Ada")

    assert _tool_return(result).metadata is not None
    assert len(generator.calls) == 1
    assert room_generator.calls == []


def test_generating_model_requires_request_response_model():
    capability = a2ui_capability.A2UICapability()
    # A run whose model is not a request-response 'Model', e.g. realtime.
    ctx = SimpleNamespace(model=object())

    with pytest.raises(a2ui_capability.NoGeneratingModel):
        capability._generating_model(ctx)  # ty: ignore[invalid-argument-type]
