"""Options-only teaching metadata. Never imported as production template defaults."""

# Values are deliberately fictitious. Each option exposes only its allowed facts.
FACT_HELP = {
    "relationship": ("保留方式", "由本策略的类别与用途规则精确匹配，不由模型或文件类型猜测。", "fully_preserved"),
    "usage": ("实际使用方式", "来自本策略用途规则的使用说明；与实际素材标签一起输出。", "Continue from the final source frame."),
    "task_types": ("实际任务类型", "根据本次素材（含配套音频）的用途匹配策略规则，按出现顺序去重后连接。", "video continuation + audio reuse"),
    "reference_relationships": ("全部参考用途说明", "每条真实参考绑定套用逐镜参考用途模板，含配套音频，不依赖模型猜测。", "<Video 1>: Continue from the final frame."),
    "reference_usage": ("本镜参考用途说明", "仅包含当前镜头使用的参考；配套音频沿用所属视频的镜头范围。", "<Audio 2>: Reuse the paired audio."),
    "shot_marker": ("镜头标记", "首镜头使用首镜头标记模板，其余使用后续镜头标记模板；不再手写时间。", "[Shot 2] At 00:05.000,"),
    "timecode": ("开始时间码", "此前镜头有效时长累计，格式为分:秒.毫秒；不使用浮点秒拼接。", "00:05.000"),
    "label": ("素材标签", "来自本次冻结的参考素材绑定，不是文件名；保留它才能准确引用素材。", "<Picture 1>"),
    "subject": ("主体标识", "参考绑定中的主体标识；对白中从说话人对应的参考绑定读取，可能为空。", "<Subject 1>"),
    "category": ("素材类别", "当前参考绑定的类别；配套音频使用 audio。", "character"),
    "purpose": ("参考用途", "来自参考绑定中已声明的用途，不由模板猜测。", "identity"),
    "guidance": ("参考使用要求", "来自参考绑定的提示说明，例如保持外观一致。", "Keep the same appearance."),
    "parent_label": ("配对视频标签", "配套音频对应的视频标签；普通素材此值为空。文字不会随空值自动隐藏。", "<Video 1>"),
    "source_kind": ("参考来源类型", "普通素材为 asset；视频内音轨为 embedded_audio；独立配套音频为 paired_audio。", "asset"),
    "shots": ("使用镜头编号", "按主体在镜头中的关联生成，从 1 开始，用本策略的段内分隔符连接。", "1; 2"),
    "mode": ("生成模式", "来自本次请求并与冻结模型数量合同核对的模式。", "Ref2VA"),
    "shot_count": ("镜头数量", "本次选中的镜头数量。", "2"),
    "reference_count": ("参考标签数量", "实际参考标签总数，包含额外的配套音频标签。", "1"),
    "include_audio": ("是否输出音频", "本次音频开关，替换为 True 或 False。", "True"),
    "index": ("镜头序号", "当前镜头在本次选中镜头中的顺序，从 1 开始。", "1"),
    "start_seconds": ("镜头开始秒数", "此前所选镜头时长的累计值。", "0.0"),
    "narrative": ("镜头叙事", "当前镜头已确认的叙事内容，按平台翻译规则取值。", "A traveler arrives."),
    "action": ("镜头动作", "当前镜头已确认的动作内容。", "The traveler opens the door."),
    "camera": ("运镜描述", "当前镜头已确认的镜头运动。", "Slow push-in."),
    "dialogue": ("已组装对白", "先用对白或画外音模板组装当前镜头对白，再放入这里；关闭音频时为空。", "<Subject 1>: Hello."),
    "references": ("本镜参考标签", "当前镜头使用的参考素材标签，包含配套音频，用段内分隔符连接。", "<Picture 1>"),
    "subjects": ("主体描述", "由当前镜头的人物、场景、道具等已确认事实整理。", "A traveler in a blue coat."),
    "environment": ("环境描述", "当前镜头的环境事实。", "A quiet station."),
    "lighting": ("灯光描述", "当前镜头的灯光事实。", "Soft daylight."),
    "color_palette": ("色彩描述", "当前镜头的色彩事实。", "Blue and warm gray."),
    "voice_profiles": ("本镜人物声线", "当前镜头人物的已确认声线描述；关闭音频时为空。", "Warm low voice."),
    "continuity": ("连续性要求", "当前镜头的连续性备注。", "Keep the door open."),
    "subject_labels": ("本镜主体标识", "从当前镜头使用的参考绑定中读取主体标识。", "<Subject 1>"),
    "requires_first_frame_continuity": ("要求首帧连续", "当前镜头的首帧连续性开关，替换为 True 或 False。", "False"),
    "requires_last_frame_continuity": ("要求尾帧连续", "当前镜头的尾帧连续性开关，替换为 True 或 False。", "False"),
    "speaker_id": ("说话人标识", "当前对白记录中的稳定说话人 ID。", "speaker_1"),
    "delivery": ("说话方式", "当前对白记录中的表达方式，按平台翻译规则取值。", "calmly"),
    "text": ("对白原文", "当前对白记录的台词，不由此模板生成。", "Hello."),
    "start_ms": ("对白开始毫秒", "相对当前镜头的开始时间；未指定时从 0 开始。", "0"),
    "end_ms": ("对白结束毫秒", "相对当前镜头的结束时间；未指定时为镜头时长。", "2000"),
    "voiceover": ("是否画外音", "由对白声明或说话人的画外音身份判断，替换为 True 或 False。", "False"),
    "language": ("项目语言", "当前项目的语言设置。", "en"),
    "voice_profile": ("说话人声线", "该说话人的已确认声线描述；无法找到说话人时为空。", "Warm low voice."),
    "visual_prompt": ("视觉风格", "项目所用风格的视觉提示描述。", "Natural cinematic lighting."),
    "color_script": ("项目色彩脚本", "项目生产设置中已确认的色彩脚本。", "Cool morning tones."),
}

# description and duration_seconds have different sources for different templates.
DESCRIPTION_HELP = {
    "reference": "从关联主体已确认的视觉属性整理；未关联主体时读取参考绑定的 description。配套音频读取其音频说明。",
    "shot": "当前镜头已确认的详细描述；存在已审核生成贡献时改走已审核逐镜模板。",
    "reviewed_shot": "当前镜头已审核的详细描述生成贡献，必须保留，不是再次调用模型。",
    "sound": "当前镜头音效（优先使用已审核贡献）；不同的场景环境音也会单独套用此模板。",
    "music": "当前镜头音乐描述，优先使用已审核的音乐贡献。",
    "direction": "本次传入的全局导演补充要求。",
}
DESCRIPTION_HELP.update({key: DESCRIPTION_HELP["reference"] for key in ("entity_reference", "retention", "reference_usage")})

# key: (when used, assembly destination, empty/required semantics, example template)
FIELD_HELP = {
    "reference": ("每个参考素材各生成一段；配套音频另外生成一段。", "参考主体定义 → reference_context.subject_definitions", "必填，且必须保留 {label}。其他占位符按需要添加。", "{label} defines {subject}. Purpose: {purpose}."),
    "retention": ("每个参考素材说明需要保留什么、哪些镜头会用到。", "参考保留关系 → reference_context.retention_analysis", "可留空；不输出该条关系，整组为空时使用无保留关系说明。", "Keep {subject} from {label} in shots {shots}."),
    "summary": ("对本次生成任务输出一段概要。", "任务摘要 → reference_context.summary", "必填；按需要选择模式、数量、时长和音频开关。", "Generate {shot_count} shots over {duration_seconds} seconds. Mode: {mode}."),
    "shot": ("每个镜头输出一段；没有已审核详细描述贡献时使用。", "导演时间线 → reference_context.detailed_description", "必填，保留 {shot_marker} 和 {reference_usage}；有音频且镜头有对白时还需 {dialogue}。", "{shot_marker} {description} {action} {camera} {reference_usage} {dialogue}"),
    "reviewed_shot": ("当前镜头存在已审核的详细描述生成贡献时，替代普通逐镜模板。", "导演时间线 → reference_context.detailed_description", "必填，保留 {shot_marker} 和 {reference_usage}；有已审核贡献时保留 {description}，有音频对白时还需 {dialogue}。", "{shot_marker} {description} {reference_usage} {dialogue}"),
    "dialogue": ("开启音频时，每条普通对白各套用一次，然后填入逐镜模板的 {dialogue}。", "先放入逐镜模板的 {dialogue}，再进入导演时间线", "有对应对白时不可留空；非空模板必须保留 {text}。", "{subject} says {text} ({delivery})."),
    "voiceover_dialogue": ("开启音频时，用于标记为画外音的对白或画外音说话人。", "先放入逐镜模板的 {dialogue}，再进入导演时间线", "有对应画外音时不可留空；非空模板必须保留 {text}。", "Voiceover: {text} ({delivery})."),
    "style": ("在整组镜头描述的开头输出一次。", "导演时间线开头 → reference_context.detailed_description", "可留空，表示不输出风格开场段。", "Style: {visual_prompt} Colors: {color_script}"),
    "sound": ("把逐镜音效或场景环境音组装成声景段落。", "总体声景 → reference_context.overall_soundscape", "无须输出时可留空；存在已审核音效贡献时必须保留 {description}。", "Shot {index} sound: {description}"),
    "music": ("把逐镜音乐描述组装成音乐段落。", "非画内音乐 → reference_context.non_diegetic_music", "无须输出时可留空；存在已审核音乐贡献时必须保留 {description}。", "Shot {index} music: {description}"),
    "direction": ("存在本次全局导演补充要求时，在镜头描述后追加。", "导演时间线结尾 → reference_context.detailed_description", "有全局要求时必须能输出非空文本；用 {description} 带入实际要求。", "Direction: {description}"),
    "empty_references": ("整组没有参考素材声明时使用。", "参考主体定义 → reference_context.subject_definitions", "可留空；本段没有动态取值，只填写希望输出的固定文字。", "No reference assets were supplied."),
    "empty_retention": ("整组没有输出参考保留关系时使用。", "参考保留关系 → reference_context.retention_analysis", "可留空；本段没有动态取值。", "No reference retention requirements."),
    "silent_sound": ("本次关闭音频输出时，替代总体声景。", "总体声景 → reference_context.overall_soundscape", "可留空；填写文字不会开启音频或添加音频节点。", "No audio output."),
    "silent_music": ("本次关闭音频输出时，替代非画内音乐。", "非画内音乐 → reference_context.non_diegetic_music", "可留空；填写文字不会开启音频或添加音乐节点。", "No music output."),
}
FIELD_HELP.update({
    "entity_reference": ("参考绑定有明确主体标识时使用，普通视频和音频仍用参考素材声明。", "参考主体定义", "必填，保留 {label} 和 {subject}。", "{subject} from {label}: {description}"),
    "retention": ("每条素材及配套音频分别记录保留关系。", "参考保留关系", "必填，保留 {label} 与 {relationship}。", "{label}: {relationship}; {guidance}"),
    "reference_usage": ("每条实际参考说明用途；摘要和当前镜头都会消费，不会仅依赖模型段落。", "任务摘要与导演时间线", "必填，保留 {label} 与 {usage}。", "{label}: {usage} {guidance}"),
    "summary": ("根据实际素材用途组装概要，任务名称来自本策略规则。", "任务摘要", "必填，保留 {task_types} 与 {reference_relationships}。", "[{task_types}] {duration_seconds} seconds. {reference_relationships}"),
    "shot_opening": ("只在本次第一个镜头使用。", "逐镜模板的 {shot_marker}", "必填，保留 {index}；是否写时间由模板决定。", "[Shot {index}]"),
    "shot_transition": ("第二个及之后的镜头使用。", "逐镜模板的 {shot_marker}", "必填，保留 {index} 与 {timecode}。", "[Shot {index}] At {timecode},"),
})


def placeholder_help(template_key, names):
    rows = {}
    for name in names:
        if name == "description":
            example = {"reference": "A traveler in a blue coat.", "sound": "Footsteps on wood.",
                       "music": "Soft piano.", "direction": "Keep the action continuous."}.get(template_key, "The traveler enters the station.")
            values = ("内容描述", DESCRIPTION_HELP[template_key], example)
        elif name == "duration_seconds":
            values = ("时长（秒）", "本次总时长；视频任务按冻结工作流的帧数规则对齐后计算。" if template_key == "summary" else "当前镜头的有效时长（秒）。", "4.0")
        else:
            values = FACT_HELP[name]
        rows[name] = dict(zip(("label", "description", "example"), values, strict=True))
    return rows
