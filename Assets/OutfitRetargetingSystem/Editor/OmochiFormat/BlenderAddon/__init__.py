# SPDX-License-Identifier: GPL-3.0-or-later
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

bl_info = {
    "name": "OMOCHI Format",
    "author": "OutfitRetargetingSystem",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "File > Import/Export",
    "description": "Import/Export OMOCHI (Outfit Retargeting System) format",
    "category": "Import-Export",
}

import bpy

from . import ors_exporter
from . import ors_importer


def menu_func_import(self, context):
    self.layout.operator(ors_importer.ORS_OT_Import.bl_idname, text="OMOCHI (.omochi)")


def menu_func_export(self, context):
    self.layout.operator(ors_exporter.ORS_OT_Export.bl_idname, text="OMOCHI (.omochi)")


classes = [
    ors_exporter.ORS_OT_Export,
    ors_importer.ORS_OT_Import,
]


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
