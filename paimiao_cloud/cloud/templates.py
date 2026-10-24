from __future__ import annotations

from jinja2 import ChoiceLoader, FileSystemLoader

def splice_loader(existing, cloud_templates_dir):

    ours = FileSystemLoader(cloud_templates_dir)
    if existing is None:
        return ours
    return ChoiceLoader([existing, ours])
