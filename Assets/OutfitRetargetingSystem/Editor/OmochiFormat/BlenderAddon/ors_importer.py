# ors_importer.py - OMOCHI file importer for Blender
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
# Imports .omochi files into Blender, creating armature and mesh objects.
# The format is EXACTLY compatible with the C# OrsWriter.cs implementation.
#
# OMOCHI File Layout:
#   [4B] Magic: "ORS\0" (0x4F, 0x52, 0x53, 0x00)
#   [4B] Version: uint32 LE = 1
#   [4B] JSON chunk length: uint32 LE (length of UTF-8 JSON bytes BEFORE padding)
#   [4B] Binary chunk length: uint32 LE
#   [variable] JSON chunk (UTF-8, padded to 4-byte boundary with 0x20)
#   [variable] Binary chunk (raw data referenced by buffer_views)

import struct
import json
import os

import bpy
from bpy.types import Operator
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix, Vector, Quaternion

try:
    from . import omochi_names
except ImportError:  # run as a standalone script
    import omochi_names


# ============================================================================
# OMOCHI File Reading
# ============================================================================

def read_ors_file(filepath):
    """Read an OMOCHI file and return (metadata_dict, binary_data_bytes).

    Raises ValueError on invalid magic or unsupported version.
    """
    with open(filepath, 'rb') as f:
        # Header: 16 bytes
        magic = f.read(4)
        if magic != b'ORS\x00':
            raise ValueError(
                f"Invalid OMOCHI file magic: {magic!r} "
                f"(expected b'ORS\\x00')"
            )

        version = struct.unpack('<I', f.read(4))[0]
        if version != 1:
            raise ValueError(
                f"Unsupported OMOCHI version: {version} (supported: 1)"
            )

        json_length = struct.unpack('<I', f.read(4))[0]
        bin_length = struct.unpack('<I', f.read(4))[0]

        # JSON chunk
        json_bytes = f.read(json_length)
        if len(json_bytes) != json_length:
            raise ValueError(
                f"Unexpected EOF reading JSON chunk: got {len(json_bytes)} "
                f"bytes, expected {json_length}"
            )

        # Skip padding to 4-byte boundary
        padding = (4 - (json_length % 4)) % 4
        if padding > 0:
            f.read(padding)

        # Binary chunk
        bin_data = f.read(bin_length)
        if len(bin_data) != bin_length:
            raise ValueError(
                f"Unexpected EOF reading binary chunk: got {len(bin_data)} "
                f"bytes, expected {bin_length}"
            )

    metadata = json.loads(json_bytes.decode('utf-8').rstrip())
    return metadata, bin_data


# ============================================================================
# Binary Buffer View Reading
# ============================================================================

def read_buffer_view(buffer_views, bin_data, index):
    """Read a buffer_view by index and return the unpacked data.

    For component_count == 1: returns a flat list of values.
    For component_count > 1: returns a list of tuples, each of length
    component_count.

    Supports component_type: "float32", "uint32".
    """
    if index < 0 or index >= len(buffer_views):
        raise IndexError(
            f"buffer_view index {index} out of range "
            f"(valid: 0-{len(buffer_views) - 1})"
        )

    bv = buffer_views[index]
    offset = bv['byte_offset']
    length = bv['byte_length']
    comp_type = bv['component_type']
    comp_count = bv['component_count']
    count = bv['count']

    end = offset + length
    if end > len(bin_data):
        raise ValueError(
            f"buffer_view[{index}] exceeds binary data: "
            f"offset={offset}, length={length}, end={end}, "
            f"buffer_size={len(bin_data)}"
        )

    raw = bin_data[offset:end]

    total_elements = count * comp_count

    if comp_type == 'float32':
        expected_size = total_elements * 4
        if len(raw) < expected_size:
            raise ValueError(
                f"buffer_view[{index}]: insufficient data for float32: "
                f"need {expected_size} bytes, have {len(raw)}"
            )
        values = struct.unpack(f'<{total_elements}f', raw[:expected_size])
    elif comp_type == 'uint32':
        expected_size = total_elements * 4
        if len(raw) < expected_size:
            raise ValueError(
                f"buffer_view[{index}]: insufficient data for uint32: "
                f"need {expected_size} bytes, have {len(raw)}"
            )
        values = struct.unpack(f'<{total_elements}I', raw[:expected_size])
    else:
        raise ValueError(
            f"buffer_view[{index}]: unsupported component_type: "
            f"{comp_type!r}"
        )

    if comp_count == 1:
        return list(values)
    else:
        return [
            tuple(values[i * comp_count:(i + 1) * comp_count])
            for i in range(count)
        ]


def read_buffer_view_flat(buffer_views, bin_data, index):
    """Read a buffer_view and return a flat list of values regardless of
    component_count.

    Useful for foreach_set operations that expect flat sequences.
    """
    if index < 0 or index >= len(buffer_views):
        raise IndexError(
            f"buffer_view index {index} out of range "
            f"(valid: 0-{len(buffer_views) - 1})"
        )

    bv = buffer_views[index]
    offset = bv['byte_offset']
    length = bv['byte_length']
    comp_type = bv['component_type']
    comp_count = bv['component_count']
    count = bv['count']

    end = offset + length
    raw = bin_data[offset:end]
    total_elements = count * comp_count

    if comp_type == 'float32':
        return list(struct.unpack(f'<{total_elements}f', raw[:total_elements * 4]))
    elif comp_type == 'uint32':
        return list(struct.unpack(f'<{total_elements}I', raw[:total_elements * 4]))
    else:
        raise ValueError(
            f"buffer_view[{index}]: unsupported component_type: "
            f"{comp_type!r}"
        )


# ============================================================================
# Matrix / Quaternion / Vector Helpers
# ============================================================================

def parse_matrix4x4(values):
    """Convert a flat list of 16 floats (row-major) to a Blender Matrix.

    The C# OrsWriter stores matrices in row-major order:
        [M11, M12, M13, M14, M21, M22, M23, M24, M31, ...]

    Blender's Matrix constructor takes rows:
        Matrix((row0, row1, row2, row3))
    where each row is (col0, col1, col2, col3).

    This is a direct mapping.
    """
    if values is None or len(values) < 16:
        return Matrix.Identity(4)

    return Matrix((
        (values[0],  values[1],  values[2],  values[3]),
        (values[4],  values[5],  values[6],  values[7]),
        (values[8],  values[9],  values[10], values[11]),
        (values[12], values[13], values[14], values[15]),
    ))


def parse_vector3(values):
    """Convert a list of 3 floats to a Blender Vector."""
    if values is None or len(values) < 3:
        return Vector((0.0, 0.0, 0.0))
    return Vector((values[0], values[1], values[2]))


def parse_quaternion(values):
    """Convert a list of 4 floats [w, x, y, z] to a Blender Quaternion.

    The C# OrsWriter stores quaternions as [W, X, Y, Z], which matches
    Blender's native Quaternion((w, x, y, z)) constructor order.
    """
    if values is None or len(values) < 4:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    return Quaternion((values[0], values[1], values[2], values[3]))


def apply_edit_bone_roll_from_matrix(ebone, matrix_local):
    """Apply only the roll component from matrix_local to an edit bone.

    Blender edit bones are fundamentally defined by head, tail, and roll.
    A full 4x4 matrix cannot preserve arbitrary scale once head/tail are
    re-authored, so we recover only the stable orientation information:
    the roll around the bone axis.
    """
    if ebone is None or matrix_local is None:
        return

    bone_axis = ebone.tail - ebone.head
    if bone_axis.length < 1e-8:
        return
    bone_axis.normalize()

    rot3 = matrix_local.to_3x3()
    target_z = rot3 @ Vector((0.0, 0.0, 1.0))
    if target_z.length < 1e-8:
        return

    # align_roll becomes unstable if the target axis is parallel to the bone.
    target_z = target_z - bone_axis * target_z.dot(bone_axis)
    if target_z.length < 1e-8:
        target_x = rot3 @ Vector((1.0, 0.0, 0.0))
        if target_x.length < 1e-8:
            return
        target_z = target_x.cross(bone_axis)
        if target_z.length < 1e-8:
            return

    target_z.normalize()
    ebone.align_roll(target_z)


def _apply_average_scale_inheritance(parent_pose):
    """Replace parent_pose's column scales with their geometric mean.

    Mirrors Blender's ``BKE_bone_parent_transform_calc_from_matrices`` for
    ``INHERIT_SCALE_AVERAGE``: ``avg = cbrt(|sx*sy*sz|)`` then rescale each
    column to that uniform scale.
    """
    cols = (
        Vector((parent_pose[0][0], parent_pose[1][0], parent_pose[2][0])),
        Vector((parent_pose[0][1], parent_pose[1][1], parent_pose[2][1])),
        Vector((parent_pose[0][2], parent_pose[1][2], parent_pose[2][2])),
    )
    sx, sy, sz = cols[0].length, cols[1].length, cols[2].length
    if sx < 1e-8 or sy < 1e-8 or sz < 1e-8:
        return parent_pose.copy()
    avg = abs(sx * sy * sz) ** (1.0 / 3.0)
    rx, ry, rz = avg / sx, avg / sy, avg / sz
    result = parent_pose.copy()
    for r in range(3):
        result[r][0] *= rx
        result[r][1] *= ry
        result[r][2] *= rz
    return result


def _remove_scale_from_pose_matrix(parent_pose):
    """Normalize parent_pose's column scales to 1 (``INHERIT_SCALE_NONE``)."""
    cols = (
        Vector((parent_pose[0][0], parent_pose[1][0], parent_pose[2][0])),
        Vector((parent_pose[0][1], parent_pose[1][1], parent_pose[2][1])),
        Vector((parent_pose[0][2], parent_pose[1][2], parent_pose[2][2])),
    )
    sx, sy, sz = cols[0].length, cols[1].length, cols[2].length
    if sx < 1e-8 or sy < 1e-8 or sz < 1e-8:
        return parent_pose.copy()
    rx, ry, rz = 1.0 / sx, 1.0 / sy, 1.0 / sz
    result = parent_pose.copy()
    for r in range(3):
        result[r][0] *= rx
        result[r][1] *= ry
        result[r][2] *= rz
    return result


def calculate_pose_basis_matrix(
        bone_matrix_local,
        pose_matrix,
        parent_bone_matrix_local=None,
        parent_pose_matrix=None,
        inherit_scale='FULL'):
    """Calculate a pose-bone basis matrix from armature-space matrices.

    Blender pose evaluation is rest-relative, so when the imported edit-bone
    rest matrices are reconstructed from head/tail/roll we must first recover
    the original local basis from the OMOCHI matrices. The recovered basis can
    then be assigned to pose_bone.matrix_basis and Blender will re-evaluate it
    against the current imported rest pose.

    For ``inherit_scale`` other than ``FULL``, Blender's
    ``BKE_bone_parent_transform_apply`` uses TWO different parent transforms:

    * ``rotscale_mat = parent_pose_ADJUSTED @ offs_bone`` (AVG/NONE-rescaled)
        — drives the rotation/scale columns of the evaluated pose.
    * ``loc_mat = parent_pose @ offs_bone`` (ORIGINAL, no rescale)
        — drives the translation column of the evaluated pose.

    Inverting only with ``parent_pose_adjusted`` recovers the rotation
    correctly but produces the wrong translation whenever the parent has
    non-uniform scale (e.g. Hips→UpperLeg→LowerLeg with UpperLeg X-scale=0.9
    and AVERAGE inheritance drifts lower_leg by ~9mm and Foot by ~19mm).
    The fix is to compute two candidate basis matrices and splice the
    translation column from the ORIGINAL-parent version into the
    ADJUSTED-parent version.
    """
    if bone_matrix_local is None or pose_matrix is None:
        return None

    bone_rest_inv = bone_matrix_local.inverted_safe()

    if parent_bone_matrix_local is None or parent_pose_matrix is None:
        return bone_rest_inv @ pose_matrix

    # Basis computed with the ORIGINAL parent_pose — its translation column
    # matches what Blender produces from ``loc_mat @ basis.translation``.
    parent_pose_orig_inv = parent_pose_matrix.inverted_safe()
    basis_orig = (
        bone_rest_inv
        @ parent_bone_matrix_local
        @ parent_pose_orig_inv
        @ pose_matrix
    )

    if inherit_scale == 'FULL':
        return basis_orig

    # Basis computed with the AVG/NONE-rescaled parent_pose — its
    # rotation/scale columns match what Blender produces from
    # ``rotscale_mat @ basis``.
    if inherit_scale == 'AVERAGE':
        parent_pose_adjusted = _apply_average_scale_inheritance(parent_pose_matrix)
    else:  # 'NONE' (and treat unknown values as FULL-ish — already returned)
        parent_pose_adjusted = _remove_scale_from_pose_matrix(parent_pose_matrix)

    basis_adj = (
        bone_rest_inv
        @ parent_bone_matrix_local
        @ parent_pose_adjusted.inverted_safe()
        @ pose_matrix
    )

    # Splice: rotation/scale columns from basis_adj, translation column
    # from basis_orig.
    result = basis_adj.copy()
    result[0][3] = basis_orig[0][3]
    result[1][3] = basis_orig[1][3]
    result[2][3] = basis_orig[2][3]
    return result


# ============================================================================
# Armature Creation
# ============================================================================

def create_armature(arm_data, metadata, bin_data, obj_data):
    """Create a Blender armature from OMOCHI armature data.

    Parameters:
        arm_data: armature dict from metadata['armatures']
        metadata: full metadata dict (for buffer_views)
        bin_data: binary buffer
        obj_data: object dict from metadata['objects'] (for world_matrix)

    Returns:
        The created armature Blender object.
    """
    buffer_views = metadata.get('buffer_views', [])
    arm_name = arm_data.get('name', 'Armature')
    bones_data = arm_data.get('bones', [])
    pose_bones_data = arm_data.get('pose_bones', [])

    # Create armature data block and object
    armature_data = bpy.data.armatures.new(name=arm_name)
    arm_obj = bpy.data.objects.new(name=obj_data.get('name', arm_name), object_data=armature_data)

    # Link to active collection
    bpy.context.collection.objects.link(arm_obj)

    # Set world matrix
    world_matrix_vals = obj_data.get('world_matrix')
    if world_matrix_vals:
        arm_obj.matrix_world = parse_matrix4x4(world_matrix_vals)

    # --- Edit Mode: create bones ---
    bpy.context.view_layer.objects.active = arm_obj
    arm_obj.select_set(True)
    bpy.ops.object.mode_set(mode='EDIT')

    edit_bones = armature_data.edit_bones
    created_bones = []  # parallel to bones_data, stores edit bone references

    # First pass: create all bones with head/tail positions
    for bone_data in bones_data:
        bone_name = bone_data.get('name', 'Bone')
        ebone = edit_bones.new(bone_name)

        head = bone_data.get('head', [0, 0, 0])
        tail = bone_data.get('tail', [0, 0, 1])

        ebone.head = Vector((head[0], head[1], head[2]))
        ebone.tail = Vector((tail[0], tail[1], tail[2]))

        # Ensure bone has non-zero length (Blender requires this)
        bone_vec = ebone.tail - ebone.head
        if bone_vec.length < 1e-6:
            ebone.tail = ebone.head + Vector((0, 0, 0.01))

        created_bones.append(ebone)

    # Blender may store a bone under another name (long names are truncated,
    # duplicates get ".001"); look bones up by the name they were given.
    original_bone_names = [bd.get('name', 'Bone') for bd in bones_data]
    actual_bone_names = [eb.name for eb in created_bones]
    arm_obj[omochi_names.BONE_NAMES] = omochi_names.renamed_map(
        zip(original_bone_names, actual_bone_names))
    arm_obj[omochi_names.BONE_SET] = json.dumps(original_bone_names, ensure_ascii=False)

    # Second pass: set parent relationships (parents must exist first)
    for i, bone_data in enumerate(bones_data):
        parent_idx = bone_data.get('parent_index')
        if parent_idx is not None and 0 <= parent_idx < len(created_bones):
            created_bones[i].parent = created_bones[parent_idx]

    # Build root-to-leaf order to avoid child propagation when applying matrices
    def build_bone_order(data):
        children = {i: [] for i in range(len(data))}
        roots = []
        for i, bd in enumerate(data):
            parent_idx = bd.get('parent_index')
            if parent_idx is None or parent_idx < 0 or parent_idx >= len(data):
                roots.append(i)
            else:
                children[parent_idx].append(i)

        order = []
        visited = set()

        def visit(idx):
            if idx in visited:
                return
            visited.add(idx)
            order.append(idx)
            for child_idx in children.get(idx, []):
                visit(child_idx)

        for root_idx in roots:
            visit(root_idx)

        # Fallback for any disconnected or cyclic bones
        for i in range(len(data)):
            if i not in visited:
                visit(i)

        return order

    bone_order = build_bone_order(bones_data)

    # Third pass: recover roll/orientation from matrix_local.
    # Edit bones cannot preserve arbitrary matrix scale once head/tail are set,
    # so keep head/tail authoritative and restore only the roll component.
    for i in bone_order:
        bone_data = bones_data[i]
        matrix_local_vals = bone_data.get('matrix_local')
        if matrix_local_vals and len(matrix_local_vals) >= 16:
            mat = parse_matrix4x4(matrix_local_vals)
            ebone = created_bones[i]
            apply_edit_bone_roll_from_matrix(ebone, mat)
            bpy.context.view_layer.update()

    # Fourth pass: set inherit_scale
    for i, bone_data in enumerate(bones_data):
        inherit_scale = bone_data.get('inherit_scale', 'FULL')
        ebone = created_bones[i]
        # Blender inherit_scale values: 'FULL', 'FIX_SHEAR', 'ALIGNED',
        # 'AVERAGE', 'NONE', 'NONE_LEGACY'
        if inherit_scale in {'FULL', 'FIX_SHEAR', 'ALIGNED', 'AVERAGE',
                             'NONE', 'NONE_LEGACY'}:
            ebone.inherit_scale = inherit_scale
        else:
            ebone.inherit_scale = 'FULL'

    # Return to object mode
    bpy.ops.object.mode_set(mode='OBJECT')
    arm_obj.select_set(False)

    # --- Pose Mode: set pose bone properties ---
    if pose_bones_data:
        # Build a name -> pose_bone_data lookup
        pose_data_by_name = {}
        for pb_data in pose_bones_data:
            pb_name = pb_data.get('name', '')
            if pb_name:
                pose_data_by_name[pb_name] = pb_data

        bone_data_by_name = {}
        for bone_data in bones_data:
            bone_name = bone_data.get('name', '')
            if bone_name:
                bone_data_by_name[bone_name] = bone_data

        bpy.context.view_layer.objects.active = arm_obj
        arm_obj.select_set(True)
        bpy.ops.object.mode_set(mode='POSE')

        pose_bones_by_name = {pb.name: pb for pb in arm_obj.pose.bones}

        for i in bone_order:
            bone_name = bones_data[i].get('name', '')
            if not bone_name:
                continue
            pose_bone = pose_bones_by_name.get(actual_bone_names[i])
            if pose_bone is None:
                continue

            pb_data = pose_data_by_name.get(bone_name)
            if pb_data is None:
                continue

            basis_matrix = None
            pb_matrix_vals = pb_data.get('matrix')
            bone_matrix_local_vals = bones_data[i].get('matrix_local')

            if (pb_matrix_vals and len(pb_matrix_vals) >= 16 and
                    bone_matrix_local_vals and len(bone_matrix_local_vals) >= 16):
                parent_bone_matrix_local = None
                parent_pose_matrix = None

                parent_idx = bones_data[i].get('parent_index')
                if parent_idx is not None and 0 <= parent_idx < len(bones_data):
                    parent_bone_data = bones_data[parent_idx]
                    parent_bone_matrix_local_vals = parent_bone_data.get('matrix_local')
                    if (parent_bone_matrix_local_vals and
                            len(parent_bone_matrix_local_vals) >= 16):
                        parent_bone_matrix_local = parse_matrix4x4(
                            parent_bone_matrix_local_vals
                        )

                    parent_name = parent_bone_data.get('name', '')
                    parent_pb_data = pose_data_by_name.get(parent_name)
                    parent_pb_matrix_vals = (
                        parent_pb_data.get('matrix') if parent_pb_data else None
                    )
                    if parent_pb_matrix_vals and len(parent_pb_matrix_vals) >= 16:
                        parent_pose_matrix = parse_matrix4x4(parent_pb_matrix_vals)

                basis_matrix = calculate_pose_basis_matrix(
                    parse_matrix4x4(bone_matrix_local_vals),
                    parse_matrix4x4(pb_matrix_vals),
                    parent_bone_matrix_local,
                    parent_pose_matrix,
                    inherit_scale=bones_data[i].get('inherit_scale', 'FULL'),
                )

            if basis_matrix is not None:
                pose_bone.matrix_basis = basis_matrix
            else:
                # Fallback for older OMOCHI files: use local channels if available.
                location = pb_data.get('location')
                rot_quat = pb_data.get('rotation_quaternion')
                scale = pb_data.get('scale')

                if (location and len(location) >= 3 and
                        rot_quat and len(rot_quat) >= 4 and
                        scale and len(scale) >= 3):
                    pose_bone.matrix_basis = Matrix.LocRotScale(
                        parse_vector3(location),
                        parse_quaternion(rot_quat),
                        parse_vector3(scale),
                    )
                elif pb_matrix_vals and len(pb_matrix_vals) >= 16:
                    pose_bone.matrix = parse_matrix4x4(pb_matrix_vals)
            
            bpy.context.view_layer.update()

        bpy.ops.object.mode_set(mode='OBJECT')
        arm_obj.select_set(False)

    return arm_obj


# ============================================================================
# Mesh Creation
# ============================================================================

def create_mesh(mesh_data, metadata, bin_data, obj_data):
    """Create a Blender mesh object from OMOCHI mesh data.

    Parameters:
        mesh_data: mesh dict from metadata['meshes']
        metadata: full metadata dict (for buffer_views)
        bin_data: binary buffer
        obj_data: object dict from metadata['objects'] (for world_matrix, name)

    Returns:
        The created mesh Blender object.
    """
    buffer_views = metadata.get('buffer_views', [])
    mesh_name = mesh_data.get('name', 'Mesh')
    obj_name = obj_data.get('name', mesh_name)

    vertex_count = mesh_data.get('vertex_count', 0)
    polygon_count = mesh_data.get('polygon_count', 0)
    loop_count = mesh_data.get('loop_count', 0)

    # --- Read binary data arrays ---
    # Positions: float32 x 3 -> flat list [x0,y0,z0, x1,y1,z1, ...]
    positions_flat = read_buffer_view_flat(
        buffer_views, bin_data, mesh_data['positions']
    )

    # Loop vertex indices: uint32 x 1
    loop_vertex_indices = read_buffer_view(
        buffer_views, bin_data, mesh_data['loop_vertex_indices']
    )

    # Polygon loop starts: uint32 x 1
    polygon_loop_starts = read_buffer_view(
        buffer_views, bin_data, mesh_data['polygon_loop_starts']
    )

    # Polygon loop counts: uint32 x 1
    polygon_loop_counts = read_buffer_view(
        buffer_views, bin_data, mesh_data['polygon_loop_counts']
    )

    # --- Create mesh data block ---
    mesh = bpy.data.meshes.new(name=mesh_name)

    # Add vertices
    mesh.vertices.add(vertex_count)
    mesh.vertices.foreach_set("co", positions_flat)

    # Add loops and polygons
    mesh.loops.add(loop_count)
    mesh.polygons.add(polygon_count)

    mesh.loops.foreach_set("vertex_index", loop_vertex_indices)
    mesh.polygons.foreach_set("loop_start", polygon_loop_starts)
    mesh.polygons.foreach_set("loop_total", polygon_loop_counts)

    # Validate and update the mesh structure
    mesh.update()
    changed = mesh.validate(verbose=False)
    if changed:
        mesh.update()

    # --- Normals ---
    # Vertex normals
    normals_bv_idx = mesh_data.get('normals')
    if normals_bv_idx is not None:
        normals_flat = read_buffer_view_flat(
            buffer_views, bin_data, normals_bv_idx
        )
        if len(normals_flat) == len(mesh.vertices) * 3:
            mesh.vertices.foreach_set("normal", normals_flat)

    # Loop normals (custom split normals)
    loop_normals_bv_idx = mesh_data.get('loop_normals')
    if loop_normals_bv_idx is not None:
        loop_normals = read_buffer_view(
            buffer_views, bin_data, loop_normals_bv_idx
        )
        actual_loop_count = len(mesh.loops)
        if loop_normals and len(loop_normals) == actual_loop_count:
            try:
                if hasattr(mesh, 'use_auto_smooth'):
                    mesh.use_auto_smooth = True
                mesh.normals_split_custom_set(loop_normals)
            except RuntimeError as e:
                print(f"Warning: Could not set custom normals for "
                      f"'{mesh_name}': {e}")
        elif loop_normals and len(loop_normals) != actual_loop_count:
            print(f"Warning: Skipping custom normals for '{mesh_name}': "
                  f"normals count ({len(loop_normals)}) != "
                  f"loop count ({actual_loop_count})")

    # --- Materials ---
    materials_data = mesh_data.get('materials', [])
    for mat_data in materials_data:
        mat_name = mat_data.get('name', 'Material')
        # Reuse existing material if it has the same name, otherwise create
        mat = bpy.data.materials.get(mat_name)
        if mat is None:
            mat = bpy.data.materials.new(name=mat_name)
        mesh.materials.append(mat)

    # --- Polygon material indices ---
    if materials_data:
        poly_mat_indices = read_buffer_view(
            buffer_views, bin_data, mesh_data['polygon_material_indices']
        )
        mesh.polygons.foreach_set("material_index", poly_mat_indices)

    # --- UV Layers ---
    uv_layers_data = mesh_data.get('uv_layers', [])
    for uv_data in uv_layers_data:
        uv_name = uv_data.get('name', 'UVMap')
        uv_layer = mesh.uv_layers.new(name=uv_name)
        uv_flat = read_buffer_view_flat(
            buffer_views, bin_data, uv_data['data']
        )
        uv_layer.data.foreach_set("uv", uv_flat)

    # --- Color attributes (vertex colors, RGBA per loop) ---
    # The file stores 8-bit layers as byte / 255 (what Unity puts in
    # Mesh.colors), so byte layers go through color_srgb, which is the stored
    # byte value; float layers are stored as is.
    if hasattr(mesh, 'color_attributes'):
        for color_data in mesh_data.get('color_attributes', []):
            if color_data.get('domain', 'CORNER') != 'CORNER' or 'data' not in color_data:
                continue
            values = read_buffer_view_flat(buffer_views, bin_data, color_data['data'])
            if len(values) != len(mesh.loops) * 4:
                print(f"Warning: Skipping color attribute '{color_data.get('name')}' of "
                      f"'{mesh_name}': {len(values) // 4} colors for {len(mesh.loops)} loops")
                continue
            byte_color = color_data.get('data_type', 'BYTE_COLOR') != 'FLOAT_COLOR'
            attribute = mesh.color_attributes.new(
                name=color_data.get('name', 'Col'),
                type='BYTE_COLOR' if byte_color else 'FLOAT_COLOR',
                domain='CORNER')
            attribute.data.foreach_set('color_srgb' if byte_color else 'color', values)

    # Final update after setting all mesh data
    mesh.update()

    # --- Create the Blender object ---
    mesh_obj = bpy.data.objects.new(name=obj_name, object_data=mesh)
    bpy.context.collection.objects.link(mesh_obj)

    # Set world matrix
    world_matrix_vals = obj_data.get('world_matrix')
    if world_matrix_vals:
        mesh_obj.matrix_world = parse_matrix4x4(world_matrix_vals)

    # Names as in the file, so the exporter can write them back (see omochi_names)
    mesh[omochi_names.NAME] = mesh_name
    vertex_group_names = []
    shape_key_names = []

    # --- Vertex Groups ---
    vertex_groups_data = mesh_data.get('vertex_groups', [])
    for vg_data in vertex_groups_data:
        vg_name = vg_data.get('name', '')
        if not vg_name:
            continue

        vg = mesh_obj.vertex_groups.new(name=vg_name)
        vertex_group_names.append((vg_name, vg.name))

        vg_indices = read_buffer_view(
            buffer_views, bin_data, vg_data['vertex_indices']
        )
        vg_weights = read_buffer_view(
            buffer_views, bin_data, vg_data['weights']
        )

        # Add weights one by one using REPLACE mode
        for vi, w in zip(vg_indices, vg_weights):
            vg.add([vi], w, 'REPLACE')

    # --- Shape Keys ---
    shape_keys_data = mesh_data.get('shape_keys', [])
    if shape_keys_data:
        # First shape key becomes the Basis (reference shape)
        basis_data = shape_keys_data[0]
        basis_key = mesh_obj.shape_key_add(
            name=basis_data.get('name', 'Basis'), from_mix=False
        )
        basis_key.interpolation = 'KEY_LINEAR'
        shape_key_names.append((basis_data.get('name', 'Basis'), basis_key.name))

        # Set basis positions
        basis_positions = read_buffer_view(
            buffer_views, bin_data, basis_data['positions']
        )
        for v_idx, pos in enumerate(basis_positions):
            if v_idx < len(basis_key.data):
                basis_key.data[v_idx].co = Vector(pos)

        # Set the basis value
        basis_key.value = basis_data.get('value', 0.0)

        # Set relative key index for basis (should be None for basis)
        # Basis is always relative to itself in Blender

        # Additional shape keys
        for sk_idx in range(1, len(shape_keys_data)):
            sk_data = shape_keys_data[sk_idx]
            sk_name = sk_data.get('name', f'Key_{sk_idx}')

            key_block = mesh_obj.shape_key_add(
                name=sk_name, from_mix=False
            )
            key_block.interpolation = 'KEY_LINEAR'
            shape_key_names.append((sk_name, key_block.name))
            key_block.value = sk_data.get('value', 0.0)

            # Set shape key positions (absolute, not delta)
            sk_positions = read_buffer_view(
                buffer_views, bin_data, sk_data['positions']
            )
            for v_idx, pos in enumerate(sk_positions):
                if v_idx < len(key_block.data):
                    key_block.data[v_idx].co = Vector(pos)

            # Set relative key
            rel_key_idx = sk_data.get('relative_key_index')
            if rel_key_idx is not None:
                key_blocks = mesh_obj.data.shape_keys.key_blocks
                if 0 <= rel_key_idx < len(key_blocks):
                    key_block.relative_key = key_blocks[rel_key_idx]

    mesh_obj[omochi_names.VERTEX_GROUP_NAMES] = omochi_names.renamed_map(vertex_group_names)
    mesh_obj[omochi_names.SHAPE_KEY_NAMES] = omochi_names.renamed_map(shape_key_names)
    return mesh_obj


# ============================================================================
# Object Hierarchy Setup
# ============================================================================

def create_empty(obj_data):
    """Create a Blender empty object from OMOCHI object data.

    Parameters:
        obj_data: object dict from metadata['objects']

    Returns:
        The created empty Blender object.
    """
    obj_name = obj_data.get('name', 'Empty')
    empty_obj = bpy.data.objects.new(name=obj_name, object_data=None)
    bpy.context.collection.objects.link(empty_obj)

    # Set world matrix
    world_matrix_vals = obj_data.get('world_matrix')
    if world_matrix_vals:
        empty_obj.matrix_world = parse_matrix4x4(world_matrix_vals)

    return empty_obj


def setup_object_hierarchy(created_objects, objects_data):
    """Set up parent-child relationships and armature modifiers.

    Handles three parent types:
    - OBJECT: standard object-to-object parenting
    - BONE: parent to a specific bone within an armature

    Parameters:
        created_objects: list of Blender objects, parallel to objects_data
        objects_data: list of object dicts from metadata['objects']
    """
    # Set parent relationships
    for i, obj_data in enumerate(objects_data):
        parent_idx = obj_data.get('parent_index')
        if parent_idx is not None and 0 <= parent_idx < len(created_objects):
            parent_obj = created_objects[parent_idx]
            child_obj = created_objects[i]
            if parent_obj is not None and child_obj is not None:
                # Store current world matrix before parenting
                child_world = child_obj.matrix_world.copy()

                child_obj.parent = parent_obj

                parent_type = obj_data.get('parent_type', 'OBJECT')
                parent_bone = obj_data.get('parent_bone')
                if parent_bone and parent_obj.type == 'ARMATURE':
                    # The bone may be stored under a truncated / numbered name
                    for actual, original in omochi_names.load_map(
                            parent_obj, omochi_names.BONE_NAMES).items():
                        if original == parent_bone:
                            parent_bone = actual
                            break

                if (parent_type == 'BONE'
                        and parent_bone
                        and parent_obj.type == 'ARMATURE'
                        and parent_bone in parent_obj.data.bones):
                    child_obj.parent_type = 'BONE'
                    child_obj.parent_bone = parent_bone
                else:
                    child_obj.parent_type = 'OBJECT'

                # Restore world matrix (parenting changes it)
                child_obj.matrix_world = child_world

    # Add armature modifiers
    for i, obj_data in enumerate(objects_data):
        arm_target_idx = obj_data.get('armature_modifier_target')
        if arm_target_idx is not None and 0 <= arm_target_idx < len(created_objects):
            mesh_obj = created_objects[i]
            arm_obj = created_objects[arm_target_idx]

            if (mesh_obj is not None and arm_obj is not None
                    and mesh_obj.type == 'MESH'
                    and arm_obj.type == 'ARMATURE'):
                # Add armature modifier
                mod = mesh_obj.modifiers.new(
                    name='Armature', type='ARMATURE'
                )
                mod.object = arm_obj
                mod.use_vertex_groups = True


# ============================================================================
# Main Import Function
# ============================================================================

def import_ors(filepath):
    """Import an OMOCHI file into the current Blender scene.

    Parameters:
        filepath: path to the .omochi file

    Returns:
        Set of created Blender objects.
    """
    # Read the file
    metadata, bin_data = read_ors_file(filepath)

    objects_data = metadata.get('objects', [])
    meshes_data = metadata.get('meshes', [])
    armatures_data = metadata.get('armatures', [])

    # Deselect all objects first
    bpy.ops.object.select_all(action='DESELECT')

    # Build lookup: armature_index -> armature_data
    armature_by_index = {}
    for idx, arm_data in enumerate(armatures_data):
        armature_by_index[idx] = arm_data

    # Build lookup: mesh_index -> mesh_data
    mesh_by_index = {}
    for idx, mesh_data in enumerate(meshes_data):
        mesh_by_index[idx] = mesh_data

    # Create Blender objects in order
    # We process objects in the order they appear, which should have
    # armatures before their child meshes (as written by OrsWriter).
    created_objects = [None] * len(objects_data)

    # First pass: create armature objects
    for i, obj_data in enumerate(objects_data):
        obj_type = obj_data.get('type', '').upper()
        if obj_type == 'ARMATURE':
            arm_idx = obj_data.get('armature_index')
            if arm_idx is not None and arm_idx in armature_by_index:
                arm_data = armature_by_index[arm_idx]
                arm_obj = create_armature(
                    arm_data, metadata, bin_data, obj_data
                )
                created_objects[i] = arm_obj

    # Ensure we are in object mode and nothing is selected
    if bpy.context.object and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    bpy.ops.object.select_all(action='DESELECT')

    # Second pass: create empty objects
    for i, obj_data in enumerate(objects_data):
        obj_type = obj_data.get('type', '').upper()
        if obj_type == 'EMPTY':
            empty_obj = create_empty(obj_data)
            created_objects[i] = empty_obj

    # Third pass: create mesh objects
    for i, obj_data in enumerate(objects_data):
        obj_type = obj_data.get('type', '').upper()
        if obj_type == 'MESH':
            mesh_idx = obj_data.get('mesh_index')
            if mesh_idx is not None and mesh_idx in mesh_by_index:
                mesh_data = mesh_by_index[mesh_idx]
                mesh_obj = create_mesh(
                    mesh_data, metadata, bin_data, obj_data
                )
                created_objects[i] = mesh_obj

    # Setup hierarchy and modifiers
    setup_object_hierarchy(created_objects, objects_data)

    # Remember where each object came from, so exporting back to this file
    # keeps the names and the mesh order Unity references (see omochi_names)
    source = os.path.normcase(os.path.abspath(filepath))
    for i, obj in enumerate(created_objects):
        if obj is None:
            continue
        obj[omochi_names.SOURCE] = source
        obj[omochi_names.INDEX] = i
        obj[omochi_names.NAME] = objects_data[i].get('name', obj.name)
        mesh_idx = objects_data[i].get('mesh_index')
        if obj.type == 'MESH' and mesh_idx is not None:
            obj[omochi_names.MESH_INDEX] = mesh_idx

    # Select all created objects
    result_objects = set()
    for obj in created_objects:
        if obj is not None:
            obj.select_set(True)
            result_objects.add(obj)

    # Set the first armature (if any) as the active object
    for obj in created_objects:
        if obj is not None and obj.type == 'ARMATURE':
            bpy.context.view_layer.objects.active = obj
            break
    else:
        # No armature found; set the first object as active
        for obj in created_objects:
            if obj is not None:
                bpy.context.view_layer.objects.active = obj
                break

    return result_objects


# ============================================================================
# Blender Operator
# ============================================================================

class ORS_OT_Import(Operator, ImportHelper):
    """Import an OMOCHI file (.omochi) into the Blender scene"""

    bl_idname = "import_scene.omochi"
    bl_label = "Import OMOCHI"
    bl_description = "Import an OMOCHI (Outfit Retargeting System) model file"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}

    filename_ext = ".omochi"

    filter_glob: bpy.props.StringProperty(
        default="*.omochi",
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        try:
            result_objects = import_ors(self.filepath)
            self.report(
                {'INFO'},
                f"OMOCHI import complete: {len(result_objects)} object(s) created"
            )
            return {'FINISHED'}
        except ValueError as e:
            self.report({'ERROR'}, f"OMOCHI import failed: {e}")
            return {'CANCELLED'}
        except Exception as e:
            self.report({'ERROR'}, f"OMOCHI import failed: {e}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}

    def draw(self, context):
        pass


# ============================================================================
# Menu Integration
# ============================================================================

def menu_func_import(self, context):
    self.layout.operator(
        ORS_OT_Import.bl_idname,
        text="OMOCHI Model (.omochi)"
    )


# ============================================================================
# Registration
# ============================================================================

classes = (
    ORS_OT_Import,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
