"""Materialize a schema for code generation from installed owner packages only.

The temporary expanded document is never a schema source. Generated contract
Python types live in types/contract; runtime validation still uses owner refs.
"""
import json
import sys
from urllib.parse import urldefrag, urlparse

from boundary_contracts import load_schema


def pointer(doc, fragment):
    for key in fragment.lstrip('/').split('/') if fragment else ():
        doc = doc[key.replace('~1', '/').replace('~0', '~')]
    return doc


def document(uri):
    if uri.startswith('https://openit.example/survey/v1/'):
        import survey_schema
        return survey_schema.load_schema(uri.rsplit('/', 1)[-1].removesuffix('.json'), version=1)
    name = uri.rsplit('/', 1)[-1].removesuffix('.schema.json')
    if not name or name.startswith('.'):
        raise ValueError('unknown schema owner reference')
    return load_schema(name)


def expand(node, root, stack=()):
    if isinstance(node, list):
        return [expand(v, root, stack) for v in node]
    if not isinstance(node, dict):
        return node
    if '$ref' in node:
        uri, fragment = urldefrag(node['$ref'])
        target_root = document(uri) if uri else root
        key = (uri, fragment)
        if key in stack:
            raise ValueError('recursive schema requires a named generated type')
        target = expand(pointer(target_root, fragment), target_root, (*stack, key))
        rest = {k: v for k, v in node.items() if k not in ('$ref', '$defs', '$id', '$schema')}
        return {'allOf': [target, expand(rest, root, stack)]} if rest else target
    return {k: expand(v, root, stack) for k, v in node.items() if k not in ('$defs', '$id', '$schema')}


if __name__ == '__main__':
    name, _, fragment = sys.argv[1].partition('#')
    doc = load_schema(name)
    print(json.dumps(expand(pointer(doc, fragment), doc), ensure_ascii=False))
