# Neolithe Blender DevKit T-pose 3 示例

此示例展示 Manuka 衣服通过 MochiFitter 转换到 Neolithe Blender DevKit T-pose 素体的目标配置。它只包含素体与转换 Profile，不包含 Manuka 角色或衣服。

- 原始输入：`Assets/Neolithe Blender DevKit 1.0.0_raw_userTpose3.fbx`。手臂的当前姿势是 T-pose，但 Rest Pose 仍为 A-pose。
- 烘焙目标：`Assets/Neolithe Blender DevKit 1.0.0_raw_userTpose3_baked.fbx`；同名 `.omochi` 用于 MochiFitter 数据生成。
- Blender 文件：`Neolithe Blender DevKit 1.0.0_raw_userTpose3_baked.blend`，可用于后续权重传递。
- Profile：`Assets/OutfitRetargetingSystem/Editor/GeneratedProfiles/neolithe_blender_devkit_1_0_0_raw_usertpose3_baked/`。胸部基准为 `Default M`，臀部基准为 `M (Default)`。

在 Unity 的 Super MochiFitter 中选择衣服 Prefab，然后选择目标 `neolithe_blender_devkit_1_0_0_raw_usertpose3_baked`。来源为 Manuka 衣服时，选择 `manuka → neolithe_blender_devkit_1_0_0_raw_usertpose3_baked (Template経由)`。高跟鞋先保留原形，不要直接让整只鞋跟随脚部形态场变形。

Profile 的包裹自检最大偏差约 7.2 mm；肩颈与脚部轮廓仍有质量警告。这个数值不能代替转换后对衣服、手套和鞋子的目视检查。

形变数据 `.npz` 使用 Git LFS 存储。克隆后需要安装 Git LFS 并执行 `git lfs pull`，才能获得完整 Profile。
