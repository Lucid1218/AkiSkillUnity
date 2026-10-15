# ors_exporter.py - Blender OMOCHI Exporter
# Part of OutfitRetargetingSystem
#
# Copyright (C) 2024 OutfitRetargetingSystem Contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Exports selected Blender objects (meshes + armatures) to the OMOCHI binary format.
# The output is EXACTLY compatible with the C# OrsReader.cs implementation.
#
# OMOCHI File Layout:
#   [4B] Magic: "ORS\0" (0x4F, 0x52, 0x53, 0x00)
#   [4B] Version: uint32 LE = 1
#   [4B] JSON chunk length: uint32 LE (length of UTF-8 JSON bytes BEFORE padding)
#   [4B] Binary chunk length: uint32 LE
#   [variable] JSON chunk (UTF-8, padded to 4-byte boundary with 0x20)
#   [variable] Binary chunk (raw binary data referenced by buffer_views)

import bpy
from bpy.types import Operator
from bpy_extras.io_utils import ExportHelper
from mathutils import Matrix
import struct
import json

try:
    from . import omochi_names
except ImportError:  # run as a standalone script
    import omochi_names


# ============================================================================
# Binary Buffer Builder
# ============================================================================

class BinaryBufferBuilder:
    """
    Accumulates binary data into a single contiguous buffer and tracks
    buffer_view entries that describe each segment.

    Each Add* method appends data to the internal buffer and returns
    the buffer_view index that references it.
    """

    def __init__(self):
        self._buffer = bytearray()
        self._buffer_views = []

    @property
    def buffer_views(self):
        """Return the list of buffer_view dictionaries."""
        return self._buffer_views

    def get_buffer(self):
        """Return the accumulated binary buffer as bytes."""
        return bytes(self._buffer)

    def _add_view(self, byte_offset, byte_length, component_type,
                  component_count, count):
        """Register a new buffer_view entry and return its index."""
        index = len(self._buffer_views)
        self._buffer_views.append({
            "byte_offset": byte_offset,
            "byte_length": byte_length,
            "component_type": component_type,
            "component_count": component_count,
            "count": count,
        })
        return index

    def add_float32_vec3_array(self, vectors):
        """
        Append an array of 3-component float32 vectors (e.g. positions,
        normals). Each element is expected to be an iterable of 3 floats.

        Returns the buffer_view index.
        """
        byte_offset = len(self._buffer)
        count = len(vectors)
        for v in vectors:
            self._buffer.extend(struct.pack("<3f", v[0], v[1], v[2]))
        byte_length = len(self._buffer) - byte_offset
        return self._add_view(byte_offset, byte_length, "float32", 3, count)

    def add_float32_vec2_array(self, vectors):
        """
        Append an array of 2-component float32 vectors (e.g. UV coordinates).

        Returns the buffer_view index.
        """
        byte_offset = len(self._buffer)
        count = len(vectors)
        for v in vectors:
            self._buffer.extend(struct.pack("<2f", v[0], v[1]))
        byte_length = len(self._buffer) - byte_offset
        return self._add_view(byte_offset, byte_length, "float32", 2, count)

    def add_float32_vec4_flat(self, flat_values):
        """
        Append a flat list of float32 values as 4-component vectors
        (e.g. RGBA colors: [r0, g0, b0, a0, r1, ...]).

        Returns the buffer_view index.
        """
        byte_offset = len(self._buffer)
        count = len(flat_values) // 4
        self._buffer.extend(struct.pack("<%df" % (count * 4), *flat_values[:count * 4]))
        byte_length = len(self._buffer) - byte_offset
        return self._add_view(byte_offset, byte_length, "float32", 4, count)

    def add_float32_array(self, values):
        """
        Append an array of scalar float32 values (e.g. weights).

        Returns the buffer_view index.
        """
        byte_offset = len(self._buffer)
        count = len(values)
        for v in values:
            self._buffer.extend(struct.pack("<f", v))
        byte_length = len(self._buffer) - byte_offset
        return self._add_view(byte_offset, byte_length, "float32", 1, count)

    def add_uint32_array(self, values):
        """
        Append an array of scalar uint32 values (e.g. indices).

        Returns the buffer_view index.
        """
        byte_offset = len(self._buffer)
        count = len(values)
        for v in values:
            self._buffer.extend(struct.pack("<I", v))
        byte_length = len(self._buffer) - byte_offset
        return self._add_view(byte_offset, byte_length, "uint32", 1, count)


# ============================================================================
# Scene Data Collection
# ============================================================================

def _flatten_matrix(matrix):
    """
    Flatten a Blender 4x4 matrix into a row-major float[16] list.

    Blender's Matrix type is accessed as matrix[row][col].
    Iterating row-first yields row-major order, which matches the
    C# OrsReader.ParseMatrix4x4 expectation:
        [M11, M12, M13, M14, M21, M22, M23, M24, ...]

    The C# side constructs:
        new Matrix4x4(
            values[0], values[1], values[2], values[3],   // row 0
            values[4], values[5], values[6], values[7],   // row 1
            values[8], values[9], values[10], values[11],  // row 2
            values[12], values[13], values[14], values[15]  // row 3
        )
    """
    return [matrix[i][j] for i in range(4) for j in range(4)]


def _vec3_to_list(vec):
    """Convert a Blender Vector (3D) to a list of 3 floats."""
    return [vec[0], vec[1], vec[2]]


def _collect_objects(selected_objects):
    """
    Walk through selected Blender objects and classify them into armature,
    mesh, and empty objects. Also collects:
    - Armatures referenced via armature modifiers on mesh objects
    - Parent objects (recursively) of all collected objects

    Returns:
        armature_objects: list of Blender objects of type 'ARMATURE'
        empty_objects: list of Blender objects of type 'EMPTY'
        mesh_objects: list of Blender objects of type 'MESH'

    Ordering in the final objects list: armatures, empties, then meshes.
    This ensures parent indices are available when building child references.
    """
    collected_set = set()
    armature_objects = []
    mesh_objects = []
    empty_objects = []

    # First pass: separate by type from selection
    for obj in selected_objects:
        if obj.type == 'ARMATURE':
            if obj not in collected_set:
                armature_objects.append(obj)
                collected_set.add(obj)
        elif obj.type == 'MESH':
            if obj not in collected_set:
                mesh_objects.append(obj)
                collected_set.add(obj)
        elif obj.type == 'EMPTY':
            if obj not in collected_set:
                empty_objects.append(obj)
                collected_set.add(obj)

    # Second pass: gather armatures referenced by mesh modifiers
    for obj in mesh_objects:
        for mod in obj.modifiers:
            if mod.type == 'ARMATURE' and mod.object is not None:
                arm_obj = mod.object
                if arm_obj not in collected_set:
                    armature_objects.append(arm_obj)
                    collected_set.add(arm_obj)

    # Third pass: collect ancestor objects (parents, grandparents, etc.)
    # that aren't already collected, to preserve hierarchy
    def _collect_ancestors(obj):
        parent = obj.parent
        while parent is not None and parent not in collected_set:
            collected_set.add(parent)
            if parent.type == 'ARMATURE':
                armature_objects.append(parent)
            elif parent.type == 'MESH':
                mesh_objects.append(parent)
            else:
                empty_objects.append(parent)
            parent = parent.parent

    all_initial = list(armature_objects) + list(empty_objects) + list(mesh_objects)
    for obj in all_initial:
        _collect_ancestors(obj)

    return armature_objects, empty_objects, mesh_objects


def _get_inherit_scale_string(bone):
    """
    Convert a Blender bone's inherit_scale enum to its string representation.

    Blender 3.x+ uses bone.inherit_scale which is an enum with values like
    'FULL', 'NONE', 'FIX_SHEAR', 'ALIGNED', 'AVERAGE', 'NONE_LEGACY'.

    Falls back to 'FULL' if the attribute is not present (older Blender).
    """
    if hasattr(bone, 'inherit_scale'):
        return bone.inherit_scale
    return "FULL"


def _build_armature_data(arm_obj, warnings=None):
    """
    Build the armature JSON data structure from a Blender armature object.

    Bone names are written as they were in the imported file when Blender only
    truncated or numbered them (see omochi_names). When the bones differ from the
    imported file, a warning is appended to `warnings`.

    Returns a dict matching the OMOCHI armatures[] JSON schema:
    {
        "name": str,
        "bones": [...],
        "pose_bones": [...]
    }
    """
    armature = arm_obj.data  # bpy.types.Armature
    bone_list = list(armature.bones)
    bone_name_to_index = {bone.name: idx for idx, bone in enumerate(bone_list)}
    bone_map = omochi_names.load_map(arm_obj, omochi_names.BONE_NAMES)

    def file_name(name):
        return omochi_names.restore_from_map(name, bone_map)

    imported = arm_obj.get(omochi_names.BONE_SET)
    if imported and warnings is not None:
        try:
            imported_names = set(json.loads(imported))
        except (TypeError, ValueError):
            imported_names = None
        if imported_names is not None and imported_names != {file_name(b.name) for b in bone_list}:
            warnings.append(
                f"Bones of '{arm_obj.name}' differ from the imported file; "
                "a Unity Prefab built from that file may no longer match its skeleton.")

    # Build bones array (EditBone/Bone data - rest pose info)
    bones_data = []
    for idx, bone in enumerate(bone_list):
        parent_index = None
        if bone.parent is not None:
            parent_index = bone_name_to_index.get(bone.parent.name)

        child_indices = []
        for child in bone.children:
            child_idx = bone_name_to_index.get(child.name)
            if child_idx is not None:
                child_indices.append(child_idx)

        bones_data.append({
            "name": file_name(bone.name),
            "parent_index": parent_index,
            "child_indices": child_indices,
            "head": _vec3_to_list(bone.head_local),
            "tail": _vec3_to_list(bone.tail_local),
            "matrix_local": _flatten_matrix(bone.matrix_local),
            "inherit_scale": _get_inherit_scale_string(bone),
        })

    # Build pose_bones array (current pose info)
    pose_bones_data = []
    if arm_obj.pose is not None:
        for bone in bone_list:
            pose_bone = arm_obj.pose.bones.get(bone.name)
            if pose_bone is None:
                # Should not happen, but generate identity data as fallback
                pose_bones_data.append({
                    "name": file_name(bone.name),
                    "matrix": _flatten_matrix(Matrix.Identity(4)),
                    "location": [0.0, 0.0, 0.0],
                    "rotation_quaternion": [1.0, 0.0, 0.0, 0.0],
                    "rotation_euler": [0.0, 0.0, 0.0],
                    "scale": [1.0, 1.0, 1.0],
                    "head": _vec3_to_list(bone.head_local),
                    "tail": _vec3_to_list(bone.tail_local),
                })
                continue

            # Blender quaternion order is (w, x, y, z) which matches our spec
            quat = pose_bone.rotation_quaternion
            euler = pose_bone.rotation_euler

            pose_bones_data.append({
                "name": file_name(pose_bone.name),
                "matrix": _flatten_matrix(pose_bone.matrix),
                "location": _vec3_to_list(pose_bone.location),
                "rotation_quaternion": [quat.w, quat.x, quat.y, quat.z],
                "rotation_euler": [euler.x, euler.y, euler.z],
                "scale": _vec3_to_list(pose_bone.scale),
                "head": _vec3_to_list(pose_bone.head),
                "tail": _vec3_to_list(pose_bone.tail),
            })

    return {
        "name": armature.name,
        "bones": bones_data,
        "pose_bones": pose_bones_data,
    }


def _disable_armature_modifiers(mesh_obj):
    """
    Temporarily disable armature modifiers on a mesh object.

    `evaluated_get(...).to_mesh()` includes the current armature deformation,
    which would bake the posed vertex positions into the exported mesh. Since
    OMOCHI also exports pose_bones and re-attaches the armature modifier on
    import, that would cause the same pose to be applied twice.

    Returns:
        list[tuple]: [(modifier, show_viewport, show_render), ...]
    """
    saved_states = []
    for modifier in mesh_obj.modifiers:
        if modifier.type != 'ARMATURE':
            continue
        saved_states.append((
            modifier,
            modifier.show_viewport,
            modifier.show_render,
        ))
        modifier.show_viewport = False
        modifier.show_render = False
    return saved_states


def _restore_modifier_states(saved_states):
    """Restore modifier visibility saved by _disable_armature_modifiers()."""
    for modifier, show_viewport, show_render in saved_states:
        modifier.show_viewport = show_viewport
        modifier.show_render = show_render


def _build_mesh_data(mesh_obj, buffer_builder, depsgraph):
    """
    Build the mesh JSON data structure and write binary data to the buffer.

    Uses obj.to_mesh() to get an evaluated mesh with armature modifiers
    temporarily disabled, so non-armature modifiers are preserved without
    baking the current bone pose into the exported vertex positions. Shape
    keys are read from the original mesh data if available.

    Args:
        mesh_obj: Blender mesh object
        buffer_builder: BinaryBufferBuilder instance
        depsgraph: Blender dependency graph

    Returns a dict matching the OMOCHI meshes[] JSON schema.
    """
    # Prevent posed armature deformation from being baked into vertex data.
    armature_modifier_states = _disable_armature_modifiers(mesh_obj)
    bpy.context.view_layer.update()

    eval_obj = None
    mesh = None

    try:
        # Get evaluated mesh with non-armature modifiers applied.
        eval_obj = mesh_obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()

        # Ensure loop normals and polygon data are computed.
        # calc_normals_split() was removed in Blender 4.1+, where split
        # normals are computed automatically. Call it only if available.
        if hasattr(mesh, 'calc_normals_split'):
            mesh.calc_normals_split()

        vertex_count = len(mesh.vertices)
        polygon_count = len(mesh.polygons)
        loop_count = len(mesh.loops)

        # ---- Positions (float32 x 3) ----
        # Use Basis (first key block) positions when shape keys exist,
        # because mesh.vertices[i].co may include baked shape key effects.
        # This ensures mesh.Positions in the ORS file always stores
        # un-shape-keyed (basis) positions.
        original_mesh_data = mesh_obj.data
        if (original_mesh_data.shape_keys is not None
                and len(original_mesh_data.shape_keys.key_blocks) > 0):
            basis_kb = original_mesh_data.shape_keys.key_blocks[0]
            positions = [(basis_kb.data[vi].co.x,
                          basis_kb.data[vi].co.y,
                          basis_kb.data[vi].co.z)
                         for vi in range(min(len(basis_kb.data), vertex_count))]
            # Pad if basis has fewer vertices than evaluated mesh
            while len(positions) < vertex_count:
                positions.append((0.0, 0.0, 0.0))
        else:
            positions = [(v.co.x, v.co.y, v.co.z) for v in mesh.vertices]
        positions_bv = buffer_builder.add_float32_vec3_array(positions)

        # ---- Normals (float32 x 3) ----
        normals = [(v.normal.x, v.normal.y, v.normal.z)
                   for v in mesh.vertices]
        normals_bv = buffer_builder.add_float32_vec3_array(normals)

        # ---- Loop vertex indices (uint32 x 1) ----
        loop_vertex_indices = [loop.vertex_index for loop in mesh.loops]
        loop_vi_bv = buffer_builder.add_uint32_array(loop_vertex_indices)

        # ---- Loop normals (float32 x 3) ----
        loop_normals = [(loop.normal.x, loop.normal.y, loop.normal.z)
                        for loop in mesh.loops]
        loop_normals_bv = buffer_builder.add_float32_vec3_array(loop_normals)

        # ---- Polygon loop starts (uint32 x 1) ----
        polygon_loop_starts = [poly.loop_start for poly in mesh.polygons]
        poly_starts_bv = buffer_builder.add_uint32_array(polygon_loop_starts)

        # ---- Polygon loop counts (uint32 x 1) ----
        polygon_loop_counts = [poly.loop_total for poly in mesh.polygons]
        poly_counts_bv = buffer_builder.add_uint32_array(polygon_loop_counts)

        # ---- Polygon material indices (uint32 x 1) ----
        polygon_mat_indices = [poly.material_index
                               for poly in mesh.polygons]
        poly_mat_bv = buffer_builder.add_uint32_array(polygon_mat_indices)

        # ---- UV layers ----
        uv_layers_data = []
        for uv_layer in mesh.uv_layers:
            uv_coords = [(d.uv[0], d.uv[1]) for d in uv_layer.data]
            uv_bv = buffer_builder.add_float32_vec2_array(uv_coords)
            uv_layers_data.append({
                "name": uv_layer.name,
                "data": uv_bv,
            })

        # ---- Color attributes (vertex colors, RGBA per loop) ----
        # 8-bit layers are written as byte / 255 (color_srgb is the stored
        # byte value; Unity puts that in Mesh.colors), float layers as stored.
        color_attributes_data = []
        for attribute in getattr(mesh, 'color_attributes', []):
            if attribute.domain not in {'CORNER', 'POINT'}:
                continue
            byte_color = attribute.data_type == 'BYTE_COLOR'
            values = [0.0] * (len(attribute.data) * 4)
            attribute.data.foreach_get('color_srgb' if byte_color else 'color', values)
            if attribute.domain == 'POINT':
                values = [c for vi in loop_vertex_indices for c in values[vi * 4:vi * 4 + 4]]
            color_attributes_data.append({
                "name": attribute.name,
                "domain": "CORNER",
                "data_type": "BYTE_COLOR" if byte_color else "FLOAT_COLOR",
                "data": buffer_builder.add_float32_vec4_flat(values),
            })

        # ---- Materials ----
        materials_data = []
        for mat_slot in mesh_obj.material_slots:
            mat_name = mat_slot.material.name if mat_slot.material else ""
            materials_data.append({"name": mat_name})
        # If no material slots, there may still be polygons with
        # material_index 0. Add a placeholder if needed.
        if len(materials_data) == 0 and polygon_count > 0:
            materials_data.append({"name": ""})

        # ---- Vertex groups (sparse format) ----
        # Build per-group sparse data by iterating vertices once,
        # instead of calling vg.weight(vi) for every vertex×group
        # (which prints "Error: Vertex not in group" to console).
        vg_count = len(mesh_obj.vertex_groups)
        per_group_indices = [[] for _ in range(vg_count)]
        per_group_weights = [[] for _ in range(vg_count)]
        for vi in range(vertex_count):
            for g in mesh_obj.data.vertices[vi].groups:
                gi = g.group
                if gi < vg_count and g.weight > 0.0:
                    per_group_indices[gi].append(vi)
                    per_group_weights[gi].append(g.weight)

        vertex_group_map = omochi_names.load_map(mesh_obj, omochi_names.VERTEX_GROUP_NAMES)
        vertex_groups_data = []
        for vg in mesh_obj.vertex_groups:
            gi = vg.index
            indices = per_group_indices[gi] if gi < vg_count else []
            weights = per_group_weights[gi] if gi < vg_count else []

            vi_bv = buffer_builder.add_uint32_array(indices)
            w_bv = buffer_builder.add_float32_array(weights)
            vertex_groups_data.append({
                "name": omochi_names.restore_from_map(vg.name, vertex_group_map),
                "vertex_indices": vi_bv,
                "weights": w_bv,
            })

        # ---- Shape keys (absolute positions) ----
        shape_keys_data = []
        shape_key_map = omochi_names.load_map(mesh_obj, omochi_names.SHAPE_KEY_NAMES)
        # Shape keys must be read from the original (non-evaluated) mesh,
        # because evaluated mesh from to_mesh() may not preserve them
        # properly depending on context.
        original_mesh = mesh_obj.data
        if original_mesh.shape_keys is not None:
            key_blocks = original_mesh.shape_keys.key_blocks
            # Build relative key name -> index mapping
            kb_name_to_idx = {kb.name: i for i, kb in enumerate(key_blocks)}

            for kb_idx, kb in enumerate(key_blocks):
                # Shape key stores absolute positions for all vertices
                sk_positions = [(kb.data[vi].co.x,
                                 kb.data[vi].co.y,
                                 kb.data[vi].co.z)
                                for vi in range(len(kb.data))]

                # Pad or truncate to match vertex count if mesh has been
                # modified by modifiers (should match in most cases)
                if len(sk_positions) < vertex_count:
                    # Pad with last known position or zeros
                    while len(sk_positions) < vertex_count:
                        sk_positions.append((0.0, 0.0, 0.0))
                elif len(sk_positions) > vertex_count:
                    sk_positions = sk_positions[:vertex_count]

                sk_bv = buffer_builder.add_float32_vec3_array(sk_positions)

                # Determine relative_key_index
                relative_key_index = None
                if kb.relative_key is not None:
                    rel_name = kb.relative_key.name
                    if rel_name in kb_name_to_idx:
                        relative_key_index = kb_name_to_idx[rel_name]

                shape_keys_data.append({
                    "name": omochi_names.restore_from_map(kb.name, shape_key_map),
                    "value": kb.value,
                    "relative_key_index": relative_key_index,
                    "positions": sk_bv,
                })

        mesh_data = {
            "name": _mesh_file_name(mesh_obj, mesh),
            "vertex_count": vertex_count,
            "polygon_count": polygon_count,
            "loop_count": loop_count,
            "positions": positions_bv,
            "normals": normals_bv,
            "loop_vertex_indices": loop_vi_bv,
            "loop_normals": loop_normals_bv,
            "polygon_loop_starts": poly_starts_bv,
            "polygon_loop_counts": poly_counts_bv,
            "polygon_material_indices": poly_mat_bv,
            "uv_layers": uv_layers_data,
            "materials": materials_data,
            "vertex_groups": vertex_groups_data,
            "shape_keys": shape_keys_data,
        }

        # Meshes without colors write no key (as before)
        if color_attributes_data:
            mesh_data["color_attributes"] = color_attributes_data

        return mesh_data

    finally:
        # Free the temporary mesh created by to_mesh() and restore the user's
        # modifier state even if export fails midway.
        if eval_obj is not None:
            eval_obj.to_mesh_clear()
        _restore_modifier_states(armature_modifier_states)
        bpy.context.view_layer.update()


def _mesh_file_name(mesh_obj, mesh):
    """
    The mesh name to write: the name it had in the imported OMOCHI file when
    Blender only truncated or numbered it (see omochi_names), else `mesh.name`.
    """
    original = mesh_obj.data.get(omochi_names.NAME)
    if original and omochi_names.is_blender_variant(mesh_obj.data.name, original):
        return original
    return mesh.name


def _unselected_imported_meshes(context, mesh_objects):
    """
    Warnings for meshes imported from the same OMOCHI file as an exported mesh
    but left out of the export. Unity references the meshes of a file by their
    index, so a file written without some of them can break those references.
    """
    sources = {o.get(omochi_names.SOURCE) for o in mesh_objects} - {None}
    if not sources:
        return []
    exported = set(mesh_objects)
    missing = [o.name for o in context.scene.objects
               if o.type == 'MESH' and o not in exported
               and o.get(omochi_names.SOURCE) in sources]
    if not missing:
        return []
    return [f"{len(missing)} mesh(es) imported from the same OMOCHI file are not exported "
            f"({', '.join(missing[:5])}{'...' if len(missing) > 5 else ''}); "
            "select every mesh when saving back over that file."]


def _find_armature_modifier_target(mesh_obj, obj_index_map):
    """
    Find the armature modifier target for a mesh object.

    Looks through the object's modifiers for an ARMATURE modifier and
    returns the index of the armature object in the global objects list.

    Args:
        mesh_obj: Blender mesh object
        obj_index_map: dict mapping Blender object -> index in objects list

    Returns:
        int or None: index of armature object, or None if not found
    """
    for mod in mesh_obj.modifiers:
        if mod.type == 'ARMATURE' and mod.object is not None:
            arm_idx = obj_index_map.get(mod.object)
            if arm_idx is not None:
                return arm_idx
    return None


def _find_parent_index(obj, obj_index_map):
    """
    Find the parent object's index in the global objects list.

    Args:
        obj: Blender object
        obj_index_map: dict mapping Blender object -> index in objects list

    Returns:
        int or None: index of parent object, or None if no parent or
                     parent not in export set
    """
    if obj.parent is not None:
        return obj_index_map.get(obj.parent)
    return None


def _get_parent_type(obj):
    """
    Get the parent type string for an object.

    In Blender, parent_type can be 'OBJECT', 'BONE', 'ARMATURE',
    'VERTEX', 'VERTEX_3', etc. We export 'OBJECT' and 'BONE'.

    Returns:
        str or None: "OBJECT", "BONE", or None if no parent
    """
    if obj.parent is None:
        return None
    if obj.parent_type == 'BONE':
        return "BONE"
    return "OBJECT"


def _get_parent_bone(obj):
    """
    Get the name of the parent bone if the object is bone-parented.

    Returns:
        str or None: bone name, or None if not bone-parented
    """
    if obj.parent is not None and obj.parent_type == 'BONE':
        return omochi_names.restore_from_map(
            obj.parent_bone, omochi_names.load_map(obj.parent, omochi_names.BONE_NAMES))
    return None


# ============================================================================
# OMOCHI File Writer
# ============================================================================

def _write_ors_file(filepath, metadata, bin_data):
    """
    Write the complete OMOCHI file to disk.

    File layout:
        Header (16 bytes):
            [4B] Magic: "ORS\0"
            [4B] Version: uint32 LE = 1
            [4B] JSON chunk length (before padding): uint32 LE
            [4B] Binary chunk length: uint32 LE
        JSON chunk:
            UTF-8 JSON, padded to 4-byte boundary with space (0x20)
        Binary chunk:
            Raw binary data
    """
    json_str = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'))
    json_bytes = json_str.encode('utf-8')
    json_length = len(json_bytes)
    bin_length = len(bin_data)

    # Padding to 4-byte boundary
    json_padding = (4 - (json_length % 4)) % 4

    with open(filepath, 'wb') as f:
        # Header (16 bytes)
        f.write(b'ORS\x00')                            # Magic
        f.write(struct.pack('<I', 1))                   # Version
        f.write(struct.pack('<I', json_length))         # JSON length
        f.write(struct.pack('<I', bin_length))           # Binary length

        # JSON chunk + padding
        f.write(json_bytes)
        f.write(b' ' * json_padding)

        # Binary chunk
        f.write(bin_data)


# ============================================================================
# Blender Operator
# ============================================================================

class ORS_OT_Export(Operator, ExportHelper):
    """Export selected objects to OMOCHI format (.omochi)"""

    bl_idname = "export_scene.omochi"
    bl_label = "Export OMOCHI"
    bl_description = "Export selected meshes and armatures to OMOCHI format"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".omochi"

    filter_glob: bpy.props.StringProperty(
        default="*.omochi",
        options={'HIDDEN'},
        maxlen=255,
    )

    export_shape_keys: bpy.props.BoolProperty(
        name="Export Shape Keys",
        description="Include shape keys (blend shapes) in the export",
        default=True,
    )

    export_vertex_groups: bpy.props.BoolProperty(
        name="Export Vertex Groups",
        description="Include vertex groups (bone weights) in the export",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return context.selected_objects is not None and len(
            context.selected_objects) > 0

    def execute(self, context):
        return self._export(context)

    def _export(self, context):
        """Main export logic."""
        selected = list(context.selected_objects)

        if not selected:
            self.report({'ERROR'}, "No objects selected for export")
            return {'CANCELLED'}

        # Classify objects
        armature_objects, empty_objects, mesh_objects = (
            _collect_objects(selected)
        )

        if not armature_objects and not mesh_objects and not empty_objects:
            self.report({'ERROR'},
                        "No exportable objects found in selection")
            return {'CANCELLED'}

        # Build global object index map.
        # Order: armatures, empties, meshes.
        # Objects imported from an OMOCHI file go back in their file order: Unity
        # references the meshes as "mesh/<name>/<index in meshes>" (see omochi_names)
        armature_objects.sort(key=lambda o: omochi_names.file_order_key(o, omochi_names.INDEX))
        empty_objects.sort(key=lambda o: omochi_names.file_order_key(o, omochi_names.INDEX))
        mesh_objects.sort(key=lambda o: omochi_names.file_order_key(o, omochi_names.MESH_INDEX))
        warnings = _unselected_imported_meshes(context, mesh_objects)

        all_blender_objects = armature_objects + empty_objects + mesh_objects
        obj_index_map = {obj: idx for idx, obj in
                         enumerate(all_blender_objects)}

        # Initialize buffer builder
        buffer_builder = BinaryBufferBuilder()

        # Get dependency graph for mesh evaluation
        depsgraph = context.evaluated_depsgraph_get()

        # ---- Build armature data ----
        armatures_json = []
        armature_data_index_map = {}  # blender obj -> armature data index
        for arm_obj in armature_objects:
            arm_data = _build_armature_data(arm_obj, warnings)
            armature_data_index_map[arm_obj] = len(armatures_json)
            armatures_json.append(arm_data)

        # ---- Build mesh data ----
        meshes_json = []
        mesh_data_index_map = {}  # blender obj -> mesh data index
        for mesh_obj in mesh_objects:
            try:
                mesh_data = _build_mesh_data(mesh_obj, buffer_builder,
                                             depsgraph)

                # Optionally strip shape keys
                if not self.export_shape_keys:
                    mesh_data["shape_keys"] = []

                # Optionally strip vertex groups
                if not self.export_vertex_groups:
                    mesh_data["vertex_groups"] = []

                mesh_data_index_map[mesh_obj] = len(meshes_json)
                meshes_json.append(mesh_data)
            except Exception as e:
                self.report({'WARNING'},
                            f"Failed to export mesh '{mesh_obj.name}': {e}")
                continue

        # ---- Build objects array ----
        objects_json = []
        used_object_names = set()
        for obj in all_blender_objects:
            obj_entry = {
                "name": omochi_names.restore(
                    obj.name, obj.get(omochi_names.NAME), used_object_names),
                "type": obj.type,  # "ARMATURE", "MESH", or "EMPTY"
                "world_matrix": _flatten_matrix(obj.matrix_world),
                "mesh_index": None,
                "armature_index": None,
                "parent_index": _find_parent_index(obj, obj_index_map),
                "parent_type": _get_parent_type(obj),
                "parent_bone": _get_parent_bone(obj),
                "armature_modifier_target": None,
            }

            if obj.type == 'ARMATURE':
                obj_entry["armature_index"] = armature_data_index_map.get(obj)
            elif obj.type == 'MESH':
                obj_entry["mesh_index"] = mesh_data_index_map.get(obj)
                obj_entry["armature_modifier_target"] = (
                    _find_armature_modifier_target(obj, obj_index_map)
                )
            # EMPTY objects have no additional data fields

            objects_json.append(obj_entry)

        # ---- Build JSON metadata ----
        metadata = {
            "version": 1,
            "generator": "OMOCHI Blender Exporter v1.0",
            "coordinate_system": "blender_z_up",
            "buffer_views": buffer_builder.buffer_views,
            "objects": objects_json,
            "meshes": meshes_json,
            "armatures": armatures_json,
        }

        # ---- Write file ----
        bin_data = buffer_builder.get_buffer()
        try:
            _write_ors_file(self.filepath, metadata, bin_data)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to write OMOCHI file: {e}")
            return {'CANCELLED'}

        obj_count = len(objects_json)
        mesh_count = len(meshes_json)
        arm_count = len(armatures_json)
        empty_count = len(empty_objects)
        for warning in warnings:
            self.report({'WARNING'}, warning)
        self.report(
            {'INFO'},
            f"Exported OMOCHI: {obj_count} objects "
            f"({mesh_count} meshes, {arm_count} armatures, "
            f"{empty_count} empties) to {self.filepath}"
        )

        return {'FINISHED'}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_shape_keys")
        layout.prop(self, "export_vertex_groups")


# ============================================================================
# Blender Registration
# ============================================================================

def menu_func_export(self, context):
    self.layout.operator(ORS_OT_Export.bl_idname, text="OMOCHI (.omochi)")


def register():
    bpy.utils.register_class(ORS_OT_Export)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    bpy.utils.unregister_class(ORS_OT_Export)


if __name__ == "__main__":
    register()
