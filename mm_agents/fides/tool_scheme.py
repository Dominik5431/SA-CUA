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
    

def make_custom_generator(variables: list[str]) -> type[GenerateJsonSchema]:
    class CustomSchemaGeneratorVar(GenerateJsonSchema):
        def generate(self, schema: CoreSchema, mode:JsonSchemaMode ='validation'):
            json_schema = super().generate(schema, mode=mode)
            json_schema.pop("title", None)
            for prop in json_schema.get("properties", {}).values():
                prop.pop("title", None)
            json_schema["additionalProperties"] = False
            assert "properties" in json_schema, "No properties found in JSON schema"
            orig_properties = json_schema["properties"]
            new_properties = {}
            for prop_name, prop_schema in orig_properties.items():
                if prop_schema.get("type") == "array":
                    value_schema = {
                        "type": "array",
                        "items": prop_schema["items"]
                    }
                else:
                    value_schema = {
                        "type": prop_schema.get("type", "string")
                    }
                description = prop_schema.get("description", "")
                new_properties[prop_name] = {
                    "description": description,
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {
                                "kind":  { "type": "string", "const": "value" },
                                "value": value_schema,
                            },
                            "required": ["kind", "value"],
                            "additionalProperties": False
                        },
                        {
                            "type": "object",
                            "properties": {
                                "kind":  { "type": "string", "const": "variable_name" },
                                "value": { "type": "string", **({"enum": variables} if variables else {}) }
                            },
                            "required": ["kind", "value"],
                            "additionalProperties": False
                        }
                    ]
                }
                json_schema["properties"] = new_properties
                json_schema["additionalProperties"] = False

            return json_schema
        
    return CustomSchemaGeneratorVar

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

    def to_dict_var(self, variables: list[str]) -> ChatCompletionToolParam:
        model_json_schema = self.parameter_model.model_json_schema(schema_generator=make_custom_generator(variables))
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
