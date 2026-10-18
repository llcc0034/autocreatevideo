"""Read-only source provenance help; never a resolver or a frozen contract.

Keys and labels remain owned by SourceSelector. This catalog explains where
facts are maintained and how explicit execution wiring supplies them.
"""
from studio.models import PromptProfileFieldRule


def prompt_source_options(selectors):
    labels = dict(PromptProfileFieldRule.SourceSelector.choices)
    result = []
    for key in selectors:
        label = labels[key]
        if key == "fixed.value":
            group = "本规则固定内容"
            location = "系统设置 → 提示词策略 → 对应策略 → 字段规则 → 本规则固定值"
            description = "固定内容只属于此规则，与字段字典及其他规则无关；发布策略时冻结。"
            usage = "不生成本次输入接线。检查当前策略版本的 fixed_value；缺失默认值需在非固定来源规则中另行启用。"
        elif key == "projection.output":
            group = "平台派生结果"
            location = "提示词策略 → 来源投影配置与模板／字段规则 → 确定性原语"
            description = "由该规则选定的原语根据已冻结的参考素材、单元事实及模板生成，不是可自由填写的业务字段。"
            usage = "在执行实例配置参考上下文输入；平台按规则产生此输出，不能把来源标识当作待填正文。"
        elif key == "request.structured_input":
            group = "本次结构化输入"
            location = "提示词策略 → 输入结构版本与字段规则路径；执行准备 → 对应结构化输入"
            description = "从确切输入结构目录选择字段路径；一个根输入可供多条规则读取，不为新字段新增来源枚举。"
            usage = "策略声明结构，实例派生同一输入；实际值可来自明确请求或配方装配。路径可供原值装配及模型生成／仅翻译；参考贡献从每个单元的 structured_input 读取同一路径，并由选定原语消费采用结果。不自动加入整份输入，不把对象转成文字，也不从旧镜头列补齐。"
        elif key.startswith("request.") and not key.startswith("request.h3_ref2va."):
            group = "本次请求输入"
            location = "项目工作台或素材生成／Smoke → 创建执行准备 → 所选实例的对应输入框"
            description = f"本次明确提供的“{label}”。输入框由精确策略派生，并非按字段同名自动读取业务内容。"
            usage = "新草稿按策略自动生成此输入，不必在实例重复接线；在创建准备时提供内容。与物理槽位复用时仍需明确映射。可用的填写、引用或输出选择方式以该版本输入合同为准；保存后冻结，不自动采用其他任务的最新输出。"
            if key == PromptProfileFieldRule.SourceSelector.REQUEST_REFERENCE:
                description = "本次明确选择的参考素材，不属于自动派生的文字或数值输入。"
                usage = "在执行实例明确配置素材槽位与参考用途，再在创建准备时选择实际素材；不自动选择文件或其他任务的最新输出。"
            if key == "request.theme":
                location = "创建执行准备 → 一句话梗概（以该版本输入合同为准）；可预览引用新建连续剧／项目设定的一句话梗概"
                description = "本次主题或一句话梗概。点击“使用项目梗概”并确认填入后才引用项目内容，可继续修改。"
            elif key == "request.style":
                location = "创建执行准备 → 视觉风格（以该版本输入合同为准）；可引用项目所选风格的视觉提示词"
                description = "本次风格要求。点击“使用项目视觉风格”后预览复制；只取视觉提示词，不附带简介、负面词或人物库。"
        elif key.startswith("reviewed_visual_rules."):
            group = "上游已审核输出"
            location = "项目生产工作台 → Bible 与影视规则 → 逐项审核影视规则"
            description = f"本轮影视规则已审核采用的“{label.removeprefix('已审核')}”，不是模型原始候选，也不是未经审核的项目现值。"
            usage = "实例自动派生项目范围输入。人工准备中读取并确认本轮审核版本；配方必须声明更早的影视规则审核步骤。保存时核对权限、审核决定及现值，冻结字段、原候选、任务及审核摘要；未审核、已变更或跨运行不可引用。组合Smoke可明确选择本人来源轮次及参考步骤，再预览确认审核字段；仅获只读来源权限，不执行生产步骤。基础资源Smoke不提供生产审核上下文。"
        elif key.startswith("project."):
            group = "项目事实"
            location = "连续剧项目 → 新建连续剧／项目设定 → " + label
            if key == "project.master_script":
                location = "项目工作台 → 剧本 → 原稿、已确认剧本修订及明确采用记录"
            description = f"项目的“{label}”语义，不等于本次请求主题，也不表示已自动关联某个项目。"
            usage = "执行实例仍须明确接线并提供对应项目内容或受控来源；仅选择这个标识不会查询当前项目。已有内容须满足该入口的范围、权限与修订要求。"
            if key == "project.logline":
                usage = "新实例草稿自动派生项目梗概输入与版本化读取合同。创建准备时选择有权读取的项目、预览并确认；冻结该次正文、来源对象、更新时间和摘要，不跟随项目后续修改。不接受手工冒充项目事实。"
        elif key.startswith("style."):
            group = "视觉风格记录"
            location = "风格素材库／新建连续剧 → 管理视觉风格 → 编辑风格 → " + label
            description = f"风格记录的“{label}”；风格名称、简介、视觉提示词和负面提示词是不同内容。"
            usage = "执行实例须明确提供此来源；不会因为项目选了风格就自动添加。受控媒体路径使用已确认译文时，以冻结来源为准。"
            if key == "style.visual_prompt":
                usage = "新实例草稿自动派生输入。创建准备时沿明确项目读取关联风格的视觉提示词；预览确认后冻结，不附加简介、负面词或完整剧本。需要可编辑副本时改用 request.style。"
        elif key.startswith("shot."):
            group = "镜头事实"
            location = "连续剧项目 → 剧集与分镜 → 对应镜头编辑 → " + label
            description = f"明确镜头范围内的“{label}”；不是任意任务的最新结果，也不是全剧自动汇总。"
            usage = "执行实例须接线并明确镜头/已审核上下文；多镜头、对白和翻译的暴露范围以所选合同为准，不能仅凭标识推断。"
        else:
            raise ValueError(f"提示词来源缺少使用说明：{key}")
        result.append({"value": key, "label": label, "group_label": group,
            "description": description, "record_location": location, "execution_usage": usage})
    return result
