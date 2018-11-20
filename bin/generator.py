#!/usr/bin/env python
# -*- coding: utf-8

import os
from collections import namedtuple
from pprint import pprint

import appdirs
import requests
from cachecontrol import CacheControl
from cachecontrol.caches import FileCache
from jinja2 import Environment, FileSystemLoader

SPEC_URL = "https://raw.githubusercontent.com/kubernetes/kubernetes/release-1.12/api/openapi-spec/swagger.json"
HTTP_CLIENT_SESSION = CacheControl(requests.session(), cache=FileCache(appdirs.user_cache_dir("k8s-generator")))
TYPE_MAPPING = {
    "integer": "int",
    "float": "float",
    "number": "float",
    "long": "int",
    "double": "float",
    "array": "list",
    "map": "dict",
    "boolean": "bool",
    "string": "six.text_type",
    "date": "date",
    "DateTime": "datetime",
    "object": "dict",
    "file": "file",
    "binary": "bytes",
    "ByteArray": "bytes",
    "UUID": "str",
}

GVK = namedtuple("GVK", ("group", "version", "kind"))
Field = namedtuple("Field", ("name", "description", "type", "ref"))
Definition = namedtuple("Definition", ("name", "description", "fields", "gvks"))


# Operation = namedtuple("Operation", ("path", "action", "description"))
# Model = namedtuple("Model", ("definition", "operations"))


class Primitive(object):
    def __init__(self, type):
        self.name = TYPE_MAPPING.get(type, type)


class Child(object):
    @property
    def parent_ref(self):
        return self.ref.rsplit(".", 1)[0]


class Module(namedtuple("Module", ("ref", "name", "imports", "models")), Child):
    pass


class Model(namedtuple("Model", ("ref", "definition",)), Child):
    @property
    def name(self):
        return self.definition.name


class Package(namedtuple("Package", ("ref", "modules"))):
    @property
    def path(self):
        return self.ref.replace(".", os.sep)


class Import(namedtuple("Import", ("module", "models"))):
    @property
    def names(self):
        return sorted(m.definition.name for m in self.models)


class Parser(object):
    def __init__(self, spec):
        self._spec = spec.get("definitions", {})
        self._packages = {}
        self._modules = {}
        self._models = {}

    def parse(self):
        self._parse_models()
        self._resolve_references()
        self._sort()
        return self._packages.values()

    def _parse_models(self):
        for id, item in self._spec.items():
            package_ref, module_name, def_name = _split_ref(id[len("io.k8s."):])
            package = self._get_package(package_ref)
            module = self._get_module(package, module_name)
            gvks = [GVK(**x) for x in item.get("x-kubernetes-group-version-kind", [])]
            fields = []
            for field_name, property in item.get("properties", {}).items():
                fields.append(
                    Field(field_name, property.get("description", ""), property.get("type"), property.get("$ref")))
            definition = Definition(def_name, item.get("description", ""), fields, gvks)
            model = Model(_make_ref(package.ref, module.name, def_name), definition)
            module.models.append(model)
            self._models[model.ref] = model
        print("Completed parse. Parsed {} packages, {} modules and {} models.".format(len(self._packages),
                                                                                      len(self._modules),
                                                                                      len(self._models)))

    def _get_package(self, package_ref):
        if package_ref not in self._packages:
            package = Package(package_ref, [])
            self._packages[package_ref] = package
        return self._packages[package_ref]

    def _get_module(self, package, module_name):
        ref = _make_ref(package.ref, module_name)
        if ref not in self._modules:
            module = Module(ref, module_name, [], [])
            package.modules.append(module)
            self._modules[ref] = module
        return self._modules[ref]

    def _get_model(self, package, module, def_name):
        ref = _make_ref(package.ref, module.name, def_name)
        return self._models[ref]

    def _resolve_references(self):
        for module in self._modules.values():
            imports = {}
            for model in module.models:
                self._resolve_fields(model.definition)
                for field in model.definition.fields:
                    ft = field.type
                    if isinstance(ft, Model):
                        if ft.parent_ref != model.parent_ref:
                            if ft.parent_ref not in imports:
                                package_ref, module_name, def_name = _split_ref(ft.ref)
                                package = self._get_package(package_ref)
                                ft_module = self._get_module(package, module_name)
                                imports[ft.parent_ref] = Import(ft_module, [])
                            imp = imports[ft.parent_ref]
                            if ft not in imp.models:
                                imp.models.append(ft)
            module.imports[:] = imports.values()

    def _resolve_fields(self, definition):
        new_fields = []
        for field in definition.fields:
            if field.ref:
                field_type = self._resolve_ref(field.ref)
            else:
                field_type = Primitive(field.type)
            new_fields.append(field._replace(type=field_type))
        definition.fields[:] = new_fields

    def _resolve_ref(self, ref):
        package_ref, module_name, def_name = _split_ref(ref[len("#/definitions/io.k8s."):])
        package = self._get_package(package_ref)
        module = self._get_module(package, module_name)
        model = self._get_model(package, module, def_name)
        return model

    def _sort(self):
        for package in self._packages.values():
            for module in package.modules:
                module.imports.sort(key=lambda i: i.module.ref)
                module.models[:] = self._sort_models(module.models)

    def _sort_models(self, models):
        """We need to make sure that any model that references an other model comes later in the list"""
        Node = namedtuple("Node", ("model", "dependants", "dependencies"))
        nodes = {}
        for model in models:
            node = nodes.setdefault(model.ref, Node(model, [], []))
            for field in model.definition.fields:
                if isinstance(field.type, Model) and field.type.parent_ref == model.parent_ref:
                    dep = nodes.setdefault(field.type.ref, Node(field.type, [], []))
                    node.dependencies.append(dep)
        for node in nodes.values():
            for dep in node.dependencies:
                dep.dependants.append(node)
        top_nodes = [n for n in nodes.values() if len(n.dependencies) == 0]
        models = []
        while top_nodes:
            top_node = top_nodes.pop()
            models.append(top_node.model)
            for dep in top_node.dependants:
                dep.dependencies.remove(top_node)
                if len(dep.dependencies) == 0:
                    top_nodes.append(dep)
        return models


class Generator(object):
    def __init__(self, packages, output_dir):
        self._packages = packages
        self._output_dir = output_dir
        self._env = Environment(
            loader=FileSystemLoader(os.path.dirname(__file__)),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def generate(self):
        print("Generating models in {}".format(self._output_dir))
        for package in self._packages:
            self._generate_package(package)
        for root, dirs, files in os.walk(self._output_dir):
            if "__init__.py" in files:
                continue
            with open(os.path.join(root, "__init__.py"), "w") as fobj:
                fobj.write("# Generated file")

    def _generate_package(self, package):
        package_dir = os.path.join(self._output_dir, package.path)
        if not os.path.isdir(package_dir):
            os.makedirs(package_dir)
        print("Created package {}.".format(package.ref))
        for module in package.modules:
            self._generate_module(module, package_dir)

    def _generate_module(self, module, package_dir):
        template = self._env.get_template("model.jinja2")
        module_path = os.path.join(package_dir, module.name) + ".py"
        with open(module_path, "w") as fobj:
            fobj.write(template.render(module=module))
        print("Generated module {}.".format(module.ref))


def _split_ref(s):
    s = s.replace("-", "_")
    return s.rsplit(".", 2)


def _make_ref(*args):
    return ".".join(args)


def main():
    resp = HTTP_CLIENT_SESSION.get(SPEC_URL)
    spec = resp.json()
    for key in ("paths", "definitions", "parameters", "responses", "securityDefinitions", "security", "tags"):
        print("Specification contains {} {}".format(len(spec.get(key, [])), key))
    pprint(spec["info"])
    output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "k8s", "models")
    parser = Parser(spec)
    packages = parser.parse()
    generator = Generator(packages, output_dir)
    generator.generate()


if __name__ == "__main__":
    main()