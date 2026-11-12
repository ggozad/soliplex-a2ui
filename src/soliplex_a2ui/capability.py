"""A capability that lets an agent show generated A2UI surfaces

The calling agent describes a surface and passes its data; a separate
generating agent, which alone holds the catalog, writes the components.
The data reaches the client exactly as the calling agent passed it.
"""

from __future__ import annotations

import dataclasses
import json
import typing

import pydantic
from a2ui.core.basic_catalog import BasicCatalog
from a2ui.core.processing.message_processor import MessageProcessor
from a2ui.core.processing.message_processor import MessageProcessorOptions
from a2ui.core.validation.payload_validator import STRICT_VALIDATION
from pydantic_ai import Agent
from pydantic_ai import RunContext
from pydantic_ai import ToolReturn
from pydantic_ai import capabilities as ai_capabilities
from pydantic_ai import exceptions as ai_exc
from pydantic_ai import models as ai_models
from pydantic_ai import toolsets as ai_toolsets

import soliplex_a2ui

PROTOCOL_VERSION = "1.0"
DEFAULT_MAX_RETRIES = 3
_OUTPUT_RETRIES_EXHAUSTED = "Exceeded maximum output retries"

_CATALOG = BasicCatalog(PROTOCOL_VERSION)
GENERATING_INSTRUCTIONS = (
    "You design A2UI user interfaces. Given a description and a data "
    "model, return the components of one surface, as a flat list in the "
    f"A2UI {PROTOCOL_VERSION} format of the catalog below.\n"
    "- One component has the id 'root'; every other component is reached "
    "from it through 'child' or 'children'.\n"
    "- Show values from the data model by binding to them with "
    '{"@path": "/json/pointer"}; never copy values into the components.\n'
    "- Bind an input to a path the data model does not have when the user "
    "is to fill it in.\n"
    "- Use only components and properties the catalog defines.\n\n"
    f"Catalog:\n{json.dumps(_CATALOG.to_json())}"
)


class NoGeneratingModel(ai_exc.UserError):
    def __init__(self, model):
        self.model = model
        super().__init__(
            f"The calling agent's model {model!r} cannot run the generating "
            "agent; set 'model' on A2UICapability"
        )


class SurfaceLayout(pydantic.BaseModel):
    components: list[dict[str, typing.Any]] = pydantic.Field(
        description="The surface's components, a flat list including 'root'",
    )


def surface_operations(
    surface_id: str,
    components: list[dict[str, typing.Any]],
    data: dict[str, typing.Any],
) -> list[dict[str, typing.Any]]:
    """The A2UI messages that create one surface with its data model"""

    def message(kind: str, **body) -> dict[str, typing.Any]:
        return {
            "version": PROTOCOL_VERSION,
            kind: {"surfaceId": surface_id, **body},
        }

    return [
        message("createSurface", catalogId=soliplex_a2ui.BASIC_CATALOG_ID),
        message("updateComponents", components=components),
        message("updateDataModel", path="/", value=data),
    ]


def validation_errors(operations: list[dict[str, typing.Any]]) -> list[str]:
    """Messages describing why a renderer would reject the operations"""
    processor = MessageProcessor(
        catalogs=[_CATALOG],
        options=MessageProcessorOptions(validation_config=STRICT_VALIDATION),
    )
    errors = processor.process_messages_with_errors(operations)
    return [error["error"]["message"] for error in errors]


@dataclasses.dataclass
class A2UICapability(ai_capabilities.AbstractCapability[typing.Any]):
    """Give an agent a 'generate_a2ui' tool that shows a surface

    The generating agent runs on 'model', or on the calling agent's model
    when 'model' is not set, and shares the calling run's usage and usage
    limits. It retries invalid layouts up to 'max_retries' times.
    """

    model: ai_models.Model | str | None = None
    max_retries: int = DEFAULT_MAX_RETRIES

    def get_toolset(self) -> ai_toolsets.FunctionToolset[typing.Any]:
        async def generate_a2ui(
            ctx: RunContext[typing.Any],
            description: str,
            data: dict[str, typing.Any],
        ) -> ToolReturn | str:
            """Show the user an interface: a card, a list, a form, ...

            Args:
                description: What the interface shows and how it is laid
                    out, in words.
                data: The values the interface shows, as a JSON object.
                    They reach the user unchanged. Leave out what the user
                    is to fill in.

            When the user acts on the interface, the action arrives as a
            user message starting with "A2UI user action:".
            """
            return await self._generate(ctx, description, data)

        return ai_toolsets.FunctionToolset([generate_a2ui])

    def _generating_model(
        self, ctx: RunContext[typing.Any]
    ) -> ai_models.Model | str:
        if self.model is not None:
            return self.model
        if isinstance(ctx.model, ai_models.Model):
            return ctx.model
        raise NoGeneratingModel(ctx.model)

    async def _generate(
        self,
        ctx: RunContext[typing.Any],
        description: str,
        data: dict[str, typing.Any],
    ) -> ToolReturn | str:
        assert ctx.tool_call_id is not None
        surface_id = f"surface_{ctx.tool_call_id}"

        agent = Agent(
            self._generating_model(ctx),
            output_type=SurfaceLayout,
            instructions=GENERATING_INSTRUCTIONS,
            retries=self.max_retries,
        )

        @agent.output_validator
        def check(layout: SurfaceLayout) -> SurfaceLayout:
            operations = surface_operations(
                surface_id, layout.components, data
            )
            errors = validation_errors(operations)
            if errors:
                raise ai_exc.ModelRetry("\n".join(errors))
            return layout

        prompt = f"{description}\n\nData model:\n{json.dumps(data)}"
        try:
            result = await agent.run(
                prompt,
                usage=ctx.usage,
                usage_limits=ctx.usage_limits,
            )
        except ai_exc.UnexpectedModelBehavior as exc:
            # pydantic-ai has no exception of its own for exhausted output
            # retries; its message is the only mark. Any other message is
            # a model or provider failure.
            if not exc.message.startswith(_OUTPUT_RETRIES_EXHAUSTED):
                raise
            return f"Could not generate a valid surface: {exc.__cause__}"

        operations = surface_operations(
            surface_id, result.output.components, data
        )
        return ToolReturn(
            return_value=(
                f"Showed surface '{surface_id}' to the user. Their "
                "interaction with it arrives as an 'A2UI user action' "
                "message."
            ),
            metadata=[
                soliplex_a2ui.surface_event(ctx.tool_call_id, operations)
            ],
        )
