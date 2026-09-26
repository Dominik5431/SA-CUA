import inspect
import json
import uuid
from dataclasses import dataclass
from docstring_parser import parse
from typing import Any, Callable, Union
from openai.types.chat import ChatCompletionToolParam
from abc import ABC, abstractmethod
from pydantic import BaseModel, Field
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode
from pydantic_core import CoreSchema
from typing import Dict, Any

class CustomSchemaGenerator(GenerateJsonSchema):
    def generate(self, schema: CoreSchema, mode:JsonSchemaMode ='validation'):
        json_schema = super().generate(schema, mode=mode)
        json_schema.pop("title", None)
        for prop in json_schema.get("properties", {}).values():
            prop.pop("title", None)
        json_schema["additionalProperties"] = False
        return json_schema
    

def make_custom_generator(variables: list[str] | None = None) -> type[GenerateJsonSchema]:
    """The planner's argument schema: every parameter as its PLAIN type.

    A variable is referenced by writing its token with a `$` in a STRING argument --
    `"$query_screen-result-1.output"` for the whole argument, or spliced into a larger
    string (`"git clone $query_screen-result-1.output"`). A literal dollar sign is `$$`.
    `LabeledBasicPlanner._expand_args` resolves them against memory and records every token
    it read, so the trust label still reaches the policy gate exactly as before.

    WHY THIS IS NOT THE OLD `kind`/`value`/`template` WRAPPER. That wrapper turned every
    parameter into a 3-way `anyOf` of objects, which cost 13,528 of the tool block's 15,694
    tokens -- 86% of the schema was packaging rather than information, and it was the single
    largest non-informative block in the whole ~44k prefix. It bought nothing the `$`
    convention does not: the planner still never types a variable's content (it types the
    token), so provenance tracking is unchanged, and `template` disappears as a concept
    because splicing is now just string interpolation.

    NON-STRING parameters (coordinates, counts, `keys`, boolean flags) are emitted plain and
    take no `$` reference. That is not a new restriction -- those values come straight out of
    the current observation, never out of redacted tool output, and the prompt already said
    they are always literals.

    *variables* is accepted for call-site compatibility and DELIBERATELY IGNORED. It used to
    become an `enum` of the live memory tokens inside every parameter, which cost ~728 prompt
    tokens per minted variable and, far worse, CHANGED THE TOOL BLOCK EVERY TIME A VARIABLE
    WAS MINTED -- invalidating the cached prefix on the 8% of calls that mint one (measured
    over 60 episodes / 2,403 planner calls). The planner learns each token's name from the
    tool-result message that minted it, and the loop recaps the in-scope variables with their
    trust labels at the TAIL of the request (LabeledPlanningLoop._variables_in_scope), where a
    change costs nothing to cache. A token that names nothing still cannot be used:
    `_expand_args` raises KeyError and the planner gets the existing "invalid arguments" retry.
    """
    return CustomSchemaGenerator


def _ensure_required_in_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure the JSON schema has a 'required' array that lists all keys in 'properties'.
    This makes the schema provider-compliant for providers that expect every property to be required.
    """
    if not isinstance(schema, dict):
        return schema

    props = schema.get("properties")
    if isinstance(props, dict) and props:
        required = schema.get("required")
        if not isinstance(required, list):
            required = []
        for key in props.keys():
            if key not in required:
                required.append(key)
        schema["required"] = required

    return schema
    
@dataclass
class Tool:
    name: str
    description: str
    callable: Callable[[type[Union[BaseModel, Any]]], type[Union[BaseModel, Any]]]
    parameter_model: type[BaseModel]
    result_model: type[BaseModel] 

    @classmethod
    def from_callable(cls, callable: Callable[[type[Union[BaseModel, Any]]], type[Union[BaseModel, Any]]]) -> "Tool":
        if not callable.__doc__:
            raise ValueError(f"Callable {callable.__name__} has no docstring")
        
        doc = parse(callable.__doc__)

        if not doc.short_description:
            raise ValueError(f"Callable {callable.__name__} has no short description")

        sig = inspect.signature(callable)
        param = next(iter(sig.parameters.values()))
        model_cls = param.annotation
        if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
            raise TypeError(f"Parameter of {callable.__name__} must be a subclass of BaseModel")
        
        if sig.return_annotation is not None:
            result_cls = sig.return_annotation
            if not (isinstance(result_cls, type) and issubclass(result_cls, BaseModel)):
                raise TypeError(f"Return type of {callable.__name__} must be a subclass of BaseModel")
        else:
            raise TypeError(f"Callable {callable.__name__} must have a return type annotation")

        return cls(
            name=callable.__name__,
            description=doc.short_description,
            callable=callable,
            parameter_model=model_cls,
            result_model=result_cls
        )


    def to_dict(self) -> ChatCompletionToolParam:
        model_json_schema = self.parameter_model.model_json_schema(schema_generator=CustomSchemaGenerator)
        model_json_schema = _ensure_required_in_schema(model_json_schema)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": model_json_schema,
                "strict": True
            }
        }

    def to_dict_var(self, variables: list[str] | None = None) -> ChatCompletionToolParam:
        """The tool-calling schema for this tool. CONSTANT for the whole episode.

        *variables* is ignored (see make_custom_generator, which also documents the `$token`
        convention this schema implies). The result is cached so the identical object is handed
        to every request: the planner's tool block must be byte-stable or the prompt-cache
        prefix breaks at the top of the system message.
        """
        cached = getattr(self, "_schema_cache", None)
        if cached is not None:
            return cached
        model_json_schema = self.parameter_model.model_json_schema(schema_generator=make_custom_generator())
        model_json_schema = _ensure_required_in_schema(model_json_schema)

        schema: ChatCompletionToolParam = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": model_json_schema,
                "strict": True
            }
        }
        self._schema_cache = schema
        return schema
