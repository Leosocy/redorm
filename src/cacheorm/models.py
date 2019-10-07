import copy

from .fields import Field, FieldAccessor, IntegerField
from .types import with_metaclass


class SchemaManager(object):
    def __init__(self, model, **kwargs):
        self.model = model

    def make_primary_cache_key(self, **query):
        return f"{self.model._meta.name}." + ".".join(
            query[field.name] for field in self.model._meta.get_primary_keys()
        )


class Metadata(object):
    def __init__(
        self, model, backend, serializer, ttl, name=None, primary_key=None, **kwargs
    ):
        self.model = model
        self.backend = backend
        self.serializer = serializer
        self.ttl = ttl
        self.name = name or model.__name__.lower()

        self.fields = {}

        for k, v in kwargs.items():
            setattr(self, k, v)
        self._additional_keys = set(kwargs.keys())

    def add_field(self, field_name, field, set_attribute=True):
        field.bind(self.model, field_name, set_attribute)
        self.fields[field.name] = field

    def set_primary_key(self, name, field):
        self.add_field(name, field)
        self.primary_key = field

    def get_primary_keys(self):
        return (self.primary_key,)

    def set_backend(self, backend):
        self.backend = backend


MODEL_BASE_NAME = "__metaclass_helper__"


class ModelBase(type):
    inheritable = {"backend", "serializer", "ttl", "primary_key"}

    def __new__(cls, name, bases, attrs):  # noqa: C901
        if name == MODEL_BASE_NAME or bases[0].__name__ == MODEL_BASE_NAME:
            return super(ModelBase, cls).__new__(cls, name, bases, attrs)

        meta_options = {}
        meta = attrs.pop("Meta", None)
        if meta:
            for k, v in meta.__dict__.items():
                if not k.startswith("_"):
                    meta_options[k] = v

        pk = getattr(meta, "primary_key", None)
        pk_name = parent_pk = None

        # Inherit any field descriptors by deep copying the underlying field
        # into the attrs of the new model, additionally see if the bases define
        # inheritable model options and swipe them.
        for b in bases:
            if not hasattr(b, "_meta"):
                continue
            base_meta = b._meta
            if parent_pk is None:
                parent_pk = copy.deepcopy(base_meta.primary_key)
            all_inheritable = cls.inheritable | base_meta._additional_keys
            for k, v in base_meta.__dict__.items():
                if k in all_inheritable and k not in meta_options:
                    meta_options[k] = v
            for k, v in b.__dict__.items():
                if k in attrs:
                    continue
                if isinstance(v, FieldAccessor) and not v.field.primary_key:
                    attrs[k] = copy.deepcopy(v)

        schema_options = meta_options.pop("schema_options", {})

        cls = super(ModelBase, cls).__new__(cls, name, bases, attrs)
        cls.__data__ = None
        cls._meta = Metadata(cls, **meta_options)
        cls._schema = SchemaManager(cls, **schema_options)

        fields = []
        for key, value in cls.__dict__.items():
            if isinstance(value, Field):
                if value.primary_key:
                    if pk:
                        raise ValueError("over-determined primary key %s." % name)
                    pk, pk_name = value, key
                else:
                    fields.append((key, value))
        if pk is None:
            if parent_pk is not False:
                pk, pk_name = (
                    (parent_pk, parent_pk.name)
                    if parent_pk is not None
                    else (IntegerField(), "id")
                )
            else:
                pk = False
        if pk is False:
            raise ValueError("required primary key %s." % name)
        cls._meta.set_primary_key(pk_name, pk)

        for name, field in fields:
            cls._meta.add_field(name, field)

        return cls


class Model(with_metaclass(ModelBase, name=MODEL_BASE_NAME)):
    def __init__(self, *args, **kwargs):
        self.__data__ = {}
        for k, v in kwargs.items():
            setattr(self, k, v)

    def get_id(self):
        return getattr(self, self._meta.primary_key.name)

    _pk = property(get_id)

    @_pk.setter
    def _pk(self, value):
        setattr(self, self._meta.primary_key.name, value)

    def _generate_insert(self, insert):
        fields = [
            field
            for field in self._meta.fields.values()
            if field not in self._meta.get_primary_keys()
        ]
        return {field.name: field.cache_value(insert[field.name]) for field in fields}

    def save(self, force_insert=False):
        query = {self._meta.primary_key.name: self._pk}
        cache_key = self._schema.make_primary_cache_key(**query)
        insert = self._generate_insert(self.__data__)
        value = self._meta.serializer.dumps(insert)
        return self._meta.backend.set(cache_key, value)

    @classmethod
    def create(cls, **query):
        inst = cls(**query)
        inst.save(force_insert=True)
        return inst

    @classmethod
    def get(cls, **query):
        cache_key = cls._schema.make_primary_cache_key(**query)
        row = cls._meta.serializer.loads(cls._meta.backend.get(cache_key))
        converted_row = {}
        for k, v in row.items():
            converted_row[k] = cls._meta.fields[k].python_value(v)
        return cls(**converted_row)

    @classmethod
    def get_by_id(cls, pk):
        query = {cls._meta.primary_key.name: pk}
        return cls.get(**query)
