"""Converting identifiers between snake_case and camelCase."""

import re


def snake_to_camel(name):
    head, *rest = name.split("_")
    return head + "".join(word.capitalize() for word in rest)


def camel_to_snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
