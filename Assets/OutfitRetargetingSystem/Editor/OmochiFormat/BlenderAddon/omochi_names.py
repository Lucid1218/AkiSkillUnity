# omochi_names.py - Keep OMOCHI names and order through a Blender round trip
# Part of OutfitRetargetingSystem
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# A Unity Prefab built from an .omochi file references its meshes as
# "mesh/<mesh name>/<mesh index>". Importing the file into Blender can change
# those names (Blender truncates long names and appends ".001" on collisions),
# and exporting writes meshes in selection order, so saving back to the same
# file could break the Prefab's mesh references.
#
# The importer stores each object's original name and index as custom
# properties (plus maps for renamed bones, vertex groups and shape keys); the
# exporter writes the original names back when the Blender name is only a
# truncated or numbered variant of it, and writes meshes in their original
# order. Names the user changed on purpose are kept.

import json
import re

SOURCE = "omochi_source"                      # object: absolute path of the imported file
NAME = "omochi_name"                          # object / mesh data: name in the file
INDEX = "omochi_index"                        # object: index in the file's objects
MESH_INDEX = "omochi_mesh_index"              # mesh object: index in the file's meshes
BONE_NAMES = "omochi_bone_names"              # armature object: {blender name: name in file}
BONE_SET = "omochi_bone_set"                  # armature object: bone names in the file (JSON list)
VERTEX_GROUP_NAMES = "omochi_vertex_group_names"  # mesh object: {blender name: name in file}
SHAPE_KEY_NAMES = "omochi_shape_key_names"    # mesh object: {blender name: name in file}

_NUMBER_SUFFIX = re.compile(r'\.\d{3,}$')

# A truncated name keeps at least this many bytes of the original (Blender's
# 63-byte limit, minus a ".001" suffix and a split multi-byte character).
_MIN_TRUNCATED_BYTES = 56


def is_blender_variant(current, original):
    """True when `current` is `original` as Blender stored it: the same name,
    truncated to Blender's length limit, and/or with a ".001" style suffix."""
    if not original:
        return False
    if current == original:
        return True
    base = _NUMBER_SUFFIX.sub('', current)
    if base == original:
        return True
    current_bytes = base.encode('utf-8')
    original_bytes = original.encode('utf-8')
    return (len(current_bytes) >= _MIN_TRUNCATED_BYTES
            and len(original_bytes) > len(current_bytes)
            and original_bytes.startswith(current_bytes))


def restore(current, original, used=None):
    """The name to export: `original` when `current` is a Blender variant of it,
    otherwise `current`. With `used`, a name already taken keeps `current`
    (e.g. a duplicated object still carries the original's properties)."""
    name = original if is_blender_variant(current, original) else current
    if used is not None:
        if name != current and name in used:
            name = current
        used.add(name)
    return name


def renamed_map(pairs):
    """JSON {blender name: name in file} for the pairs whose names differ."""
    mapping = {actual: original for original, actual in pairs if actual != original}
    return json.dumps(mapping, ensure_ascii=False)


def load_map(id_data, key):
    """The {blender name: name in file} map stored on `id_data`, or {}."""
    text = id_data.get(key) if id_data is not None else None
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def restore_from_map(current, mapping):
    """`current` written back with the name it had in the file (see renamed_map)."""
    original = mapping.get(current)
    return original if original and is_blender_variant(current, original) else current


def file_order_key(obj, key):
    """Sort key putting objects with a stored file index first, in that order."""
    index = obj.get(key)
    return (0, index) if isinstance(index, int) else (1, 0)
