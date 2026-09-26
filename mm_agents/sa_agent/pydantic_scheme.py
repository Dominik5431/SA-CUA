import builtins
from typing import Generic, TypeVar, Any, get_args
from pydantic_core import core_schema
from pydantic import BaseModel, Field, GetCoreSchemaHandler, TypeAdapter

T = TypeVar("T")

TYPE_MAP = {
    "bool": bool,
    "int": int,
    "float": float,
    "str": str,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "complex": complex,
}


class MetaValue(Generic[T]):
    def __init__(self, value: T, metadata: dict[str, Any] | None = None):
        self.value = value
        self.metadata = metadata or {}

    def __repr__(self):
        return repr(self.value)

    def __str__(self):
        return str(self.value)

    def __lt__(self, other: Any) -> bool:
        if isinstance(other, MetaValue):
            return self.value < other.value
        else:
            return self.value < other

    def __gt__(self, other: Any) -> bool:
        if isinstance(other, MetaValue):
            return self.value > other.value
        else:
            return self.value > other

    def __getattr__(self, name: str):
        return getattr(self.value, name)
    

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: GetCoreSchemaHandler) -> core_schema.CoreSchema:
        args = get_args(source)
        if args:
            inner_type = args[0]
            inner_schema = handler.generate_schema(inner_type)
        else:
            raise TypeError(f"MetaValue must be parameterized with a type, e.g., MetaValue[int]")

        instance_schema = core_schema.is_instance_schema(cls)
        
        wrap_schema = core_schema.no_info_after_validator_function(cls, inner_schema)
        
        python_schema = core_schema.union_schema([instance_schema, wrap_schema])
        
        def serialize_metavalue(mv: MetaValue) -> Any:
            return mv.value
        
        return core_schema.json_or_python_schema(
            json_schema=python_schema,
            python_schema=python_schema,
            serialization=core_schema.plain_serializer_function_ser_schema(
                serialize_metavalue,
                info_arg=False,
                return_schema=inner_schema,
            ),
        )
    

def resolve_type(type_str: str) -> type:
    typ = getattr(builtins, type_str, None)
    if isinstance(typ, type):
        return typ
    raise ValueError(f"Unsupported output_type: '{type_str}'")


def enforce_type(value: Any, type_str: str) -> Any:
    typ = resolve_type(type_str)
    adapter = TypeAdapter(typ)
    return adapter.validate_python(value)
