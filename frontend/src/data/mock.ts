import type { Asset, BibleItem, ComfySettings, Episode, SeriesProject, Shot, StylePreset, Workflow } from '@/types'

const now = new Date().toISOString()

export const stylePresets: StylePreset[] = [
  { id: 'wuxia', slug: 'wuxia', name: '武侠', englishName: 'Wuxia', category: '东方幻想', description: '凌厉剑意、江湖烟雨与写意动作美学', accent: '#f0b56a', gradient: 'linear-gradient(145deg,#4c251b,#121418 64%,#77522d)', tags: ['江湖', '剑术', '写意'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'neo-wuxia cinematic, ink-wash atmosphere, elegant martial arts choreography' },
  { id: 'xianxia', slug: 'xianxia', name: '玄幻仙侠', englishName: 'Xianxia Fantasy', category: '东方幻想', description: '仙山秘境、灵力特效与宏大宗门世界', accent: '#9f8cff', gradient: 'linear-gradient(145deg,#251d52,#15121f 58%,#583c7c)', tags: ['仙侠', '秘境', '灵力'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'epic xianxia fantasy, ethereal spiritual energy, majestic immortal realm' },
  { id: 'anime', slug: 'anime', name: '国创动漫', englishName: 'Chinese Animation', category: '动画', description: '国风造型、高能构图与细腻赛璐璐光影', accent: '#ff6a7b', gradient: 'linear-gradient(145deg,#5a182e,#151823 60%,#333d7a)', tags: ['国创', '赛璐璐', '热血'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'premium Chinese animation film, refined cel shading, dynamic composition' },
  { id: 'thriller', slug: 'thriller', name: '心理惊悚', englishName: 'Psychological Thriller', category: '悬疑惊悚', description: '压迫空间、冷峻光影与逐步失控的心理张力', accent: '#70c5b0', gradient: 'linear-gradient(145deg,#123b3a,#0e1013 58%,#344037)', tags: ['压迫感', '暗光', '反转'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'psychological thriller, oppressive negative space, cold practical lighting, subtle dread' },
  { id: 'detective', slug: 'detective', name: '民国侦探', englishName: 'Republic Detective', category: '悬疑惊悚', description: '民国租界、雨夜暗巷与层层递进的谜案', accent: '#66b5ff', gradient: 'linear-gradient(145deg,#132b43,#111318 58%,#493329)', tags: ['民国', '罪案', '雨夜'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'Republic-era Chinese detective noir, rain-soaked streets, chiaroscuro, 35mm film grain' },
  { id: 'urban-drama', slug: 'urban-drama', name: '都市剧情', englishName: 'Urban Drama', category: '现实题材', description: '现代都市、自然表演与克制真实的情感表达', accent: '#e9a86b', gradient: 'linear-gradient(145deg,#51412e,#171616 58%,#65412e)', tags: ['都市', '情感', '生活流'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'intimate urban drama, naturalistic performance, warm available light, grounded realism' },
  { id: 'cyberpunk', slug: 'cyberpunk', name: '未来科幻', englishName: 'Future Sci-Fi', category: '科幻', description: '高密度未来都市、智能科技与霓虹工业美学', accent: '#40e0d0', gradient: 'linear-gradient(145deg,#153d46,#10101a 54%,#4a155a)', tags: ['未来城', '霓虹', '科技'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'cinematic future science fiction, dense neon megacity, volumetric atmosphere, plausible technology' },
  { id: 'palace-intrigue', slug: 'palace-intrigue', name: '古装权谋', englishName: 'Palace Intrigue', category: '古装', description: '考究服化道、深宫棋局与克制锋利的权力较量', accent: '#d9b66f', gradient: 'linear-gradient(145deg,#4b331c,#17140f 62%,#5f1c18)', tags: ['朝堂', '谋略', '古装'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'Chinese palace intrigue, authentic historical costume, candlelit court, restrained political tension' },
  { id: 'post-apocalypse', slug: 'post-apocalypse', name: '末日废土', englishName: 'Post Apocalypse', category: '科幻', description: '荒芜巨构、生存小队与风沙侵蚀的末世质感', accent: '#c38c54', gradient: 'linear-gradient(145deg,#4a3828,#111311 58%,#37443b)', tags: ['废土', '生存', '遗迹'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'post-apocalyptic wasteland cinema, eroded megastructures, dust storm, survival realism' },
  { id: 'healing-fantasy', slug: 'healing-fantasy', name: '治愈奇幻', englishName: 'Healing Fantasy', category: '奇幻', description: '温柔魔法、自然秘境与抚慰人心的童话质感', accent: '#8fd1aa', gradient: 'linear-gradient(145deg,#245447,#17201d 56%,#6c5c89)', tags: ['治愈', '自然', '童话'], assetCounts: { character: 3, scene: 3, prop: 3 }, promptPrefix: 'healing fantasy cinema, gentle magic, luminous nature, poetic storybook atmosphere' }
]

const assetNames: Record<string, { character: string[]; scene: string[]; prop: string[] }> = {
  wuxia: { character: ['凌霜', '顾千山', '谢知微'], scene: ['雾竹石径', '雨夜客栈', '云海崖祠'], prop: ['玄纹剑', '密信竹筒', '门派铜令'] },
  xianxia: { character: ['云织', '烬渊', '木隐婆婆'], scene: ['云上仙门', '灵花秘林', '天宫露台'], prop: ['碧霄灵剑', '凝露丹瓶', '裂星罗盘'] },
  anime: { character: ['林柚', '周砚', '唐澄'], scene: ['雨坡旧巷', '天文社教室', '海岸废弃天文台'], prop: ['星图吊坠', '旧磁带录音机', '自制望远镜'] },
  thriller: { character: ['顾岚', '陈伯', '苏黛'], scene: ['医院地下走廊', '停电旧楼梯', '雾湖公交站'], prop: ['停摆腕表', '空牌房门钥匙', '断线红电话'] },
  detective: { character: ['沈墨兰', '许砚舟', '严鹤庭'], scene: ['无字雨街', '装饰艺术酒店', '旧剧院后台'], prop: ['折叠相机', '几何银烟盒', '封存证物袋'] },
  'urban-drama': { character: ['许晴川', '周行', '苏曼青'], scene: ['屋顶设计室', '打烊后咖啡馆', '蓝调家庭餐厅'], prop: ['金缮咖啡杯', '旧屋钥匙', '社区院落模型'] },
  cyberpunk: { character: ['季零', '罗城', '岚'], scene: ['雨夜层城市场', '自动地铁控制室', '霓雾屋顶温室'], prop: ['加密记忆晶片', '掌上信号扫描器', '折叠侦察无人机'] },
  'palace-intrigue': { character: ['宁素', '裴朔', '章太后'], scene: ['晨光朝堂', '烛影宫档', '雪关帅亭'], prop: ['无字玉牒', '玄漆官印', '绢帛地形图'] },
  'post-apocalypse': { character: ['阮青', '陆野', '叶岚'], scene: ['藤蔓高架', '风沙光伏场', '旧站安全屋'], prop: ['手摇应急电台', '便携净水罐', '荒原罗盘'] },
  'healing-fantasy': { character: ['小满', '阿听', '钟爷爷'], scene: ['老树灯笼汤铺', '发光纸船河镇', '晨光钟表阁楼'], prop: ['星光瓷碗', '蓝纸船', '新芽怀表'] }
}

const mockBibleImagePromptConfig = (styleId: string): SeriesProject['bibleImagePromptConfig'] => ({
  schemaVersion: 1,
  stylePrompt: stylePresets.find((style) => style.id === styleId)?.promptPrefix || 'cinematic production reference',
  styleNegativePrompt: '',
  compositionRules: {
    character: 'neutral full-body or three-quarter standing pose; unobstructed face; identity reference image; plain unobtrusive studio background; no other people',
    scene: 'wide establishing view; empty environment; no people or characters; clear spatial layout and architectural continuity reference',
    prop: 'three-quarter product view; neutral seamless background; centered; fully visible; no hands, no people, no duplicate objects',
  },
  negativeRules: {
    character: 'multiple people, crowd, duplicate person, obscured face, cropped head',
    scene: 'people, person, character, crowd, portrait, close-up',
    prop: 'hands, person, people, multiple objects, duplicate object, clutter',
  },
  qualitySuffix: 'high detail, consistent identity, production reference',
})

export const mockAssets: Asset[] = stylePresets.flatMap((style) => {
  const groups = assetNames[style.id]
  return (Object.keys(groups) as Array<keyof typeof groups>).flatMap((type) => groups[type].map((name, index) => ({
    id: `${style.id}-${type}-${String(index + 1).padStart(2, '0')}`,
    styleId: style.id,
    type,
    name,
    description: `${style.name}视觉体系中的${type === 'character' ? '角色定妆' : type === 'scene' ? '场景气氛' : '关键道具'}参考，已针对连续镜头一致性优化。`,
    thumbnail: `/assets/library/${style.slug}/${type}-${String(index + 1).padStart(2, '0')}.jpg`,
    tags: [style.name, type === 'character' ? '人物' : type === 'scene' ? '场景' : '道具', '一致性参考'],
    seed: 48271 + index * 131 + stylePresets.indexOf(style) * 1000
  })))
})

const newBibleItem = (id: string, name: string, description: string, assetId: string): BibleItem => ({
  id, name, description, visualPrompt: `${name}，${description}，固定面部结构与服装材质，同一角色跨镜头保持发型、年龄、体态、色彩完全一致`, referenceAssetIds: [assetId], locked: true, seed: 238410 + id.length * 17
})

const shot = (id: string, order: number, title: string, duration: number, action: string): Shot => ({
  id, order, title, mediaType: 'video', duration, mode: order === 1 ? 'FL2VA' : 'Ref2VA', framing: order === 1 ? '大全景' : '中近景', camera: order === 1 ? '缓慢推轨，轻微手持呼吸' : '低机位横移后快速推近', action, prompt: '', negativePrompt: '人物身份漂移，面部变化，多余肢体，文字水印，镜头跳切，低清晰度', characterIds: order === 1 ? ['char-shen'] : ['char-shen', 'char-qing'], sceneId: 'scene-bamboo', propIds: order === 1 ? [] : ['prop-sword'], referenceAssetIds: [], referenceBindings: { characters: [], props: [], videos: [], audios: [] }, requiresFirstFrameContinuity: order > 1, requiresLastFrameContinuity: true, audioEnabled: true, audio: { dialogue: order === 1 ? '' : '青禾（压低声音）：他们追上来了。', soundscape: '细密雨声、竹叶摩擦、远处闷雷，剑鞘轻碰衣料', music: '低沉古琴单音，随后加入克制的弦乐脉冲' }, status: order === 1 ? 'ready' : 'draft'
})

const episode = (id: string, number: number, title: string, synopsis: string): Episode => ({
  id, number, title, synopsis, status: number === 1 ? 'ready' : 'draft', multiShot: true, audioEnabled: true,
  shots: number === 1 ? [shot('shot-1-1', 1, '雨幕入局', 5, '雨夜竹林中，沈砚背对镜头独行，远处黑衣人影在闪电中短暂显现。'), shot('shot-1-2', 2, '剑未出鞘', 6, '青禾从竹影中现身示警，沈砚按住玄铁剑，水珠顺着剑柄滑落。'), shot('shot-1-3', 3, '伏兵四起', 5, '八名伏兵同时落地，沈砚抬眼，镜头环绕人物形成临战压迫。')] : [shot(`shot-${number}-1`, 1, '开场建立', 6, synopsis)],
  createdAt: now
})

export const mockProjects: SeriesProject[] = [
  {
    id: 'project-rain-sword', title: '听雨不归人', logline: '失忆剑客护送一封无法打开的密信，却发现沿途追杀者都来自他曾经建立的组织。', masterScript: '第1集：雨夜来信\n镜头1：雨夜竹林中，沈砚背对镜头独行。\n镜头2：青禾从竹影中现身示警。', isFavorite: true, styleId: 'wuxia', status: 'producing', aspectRatio: '9:16', resolution: '720p', defaultMode: 'Ref2VA',
    bibleImagePromptConfig: mockBibleImagePromptConfig('wuxia'),
    bible: {
      characters: [newBibleItem('char-shen', '沈砚', '28岁清瘦剑客，黑发高束，左眉一道浅疤，墨色窄袖长衫，沉静克制', 'wuxia-character-01'), newBibleItem('char-qing', '青禾', '24岁药师，青灰斗篷，银簪，腰间药囊，眼神敏锐', 'wuxia-character-02')],
      scenes: [newBibleItem('scene-bamboo', '竹林雨夜', '深夜冷青色竹林，斜雨、薄雾、湿润石径，远方暖色灯笼为唯一暖光', 'wuxia-scene-01')],
      props: [newBibleItem('prop-sword', '玄铁长剑', '暗哑黑色剑鞘，旧银吞口，剑柄缠深红旧布，不出现花哨装饰', 'wuxia-prop-01')],
      negativePrompt: '现代物件，服装漂移，角色年龄变化，发型变化，塑料材质，过饱和，卡通化，文字，水印', colorScript: '冷青灰雨夜为主，人物肤色自然偏冷；关键线索使用低饱和琥珀色点亮。'
    },
    episodes: [episode('episode-1', 1, '雨夜来信', '沈砚在竹林遭伏，青禾带来一封写着他名字的密信。'), episode('episode-2', 2, '无名客栈', '二人在客栈避雨，却发现所有住客都认识沈砚。'), episode('episode-3', 3, '剑冢旧梦', '密信指向雪山剑冢，沈砚第一次看见自己被抹去的过去。')], createdAt: '2026-08-01T08:00:00Z', updatedAt: now
  },
  {
    id: 'project-neon-evidence', title: '零点证词', logline: '一名失去部分记忆的法医，在每晚零点收到来自未来受害者的证词。', masterScript: '第1集：来自明天的尸检\n镜头1：零点，尚未发生的凶案报告出现在解剖台上。', isFavorite: false, styleId: 'detective', status: 'writing', aspectRatio: '9:16', resolution: '720p', defaultMode: 'FL2VA',
    bibleImagePromptConfig: mockBibleImagePromptConfig('detective'),
    bible: { characters: [newBibleItem('char-forensic', '林默', '31岁法医，短发，深色风衣，右手戴黑色手套', 'detective-character-02')], scenes: [newBibleItem('scene-archive', '旧档案室', '冷白顶灯与窗外蓝紫霓虹交叠，潮湿墙面，密集铁柜', 'detective-scene-02')], props: [newBibleItem('prop-camera', '袖珍相机', '磨损黄铜机身，镜片边缘一道裂纹', 'detective-prop-02')], negativePrompt: '明快高调光，角色变化，现代品牌标识，水印', colorScript: '钴蓝、青绿与少量钠灯橙，真相揭示时逐步抽离彩色。' },
    episodes: [episode('episode-d1', 1, '来自明天的尸检', '零点，尚未发生的凶案报告出现在解剖台上。')], createdAt: '2026-08-05T10:00:00Z', updatedAt: now
  }
]

export const mockWorkflows: Workflow[] = [
  { id: 'wf-001', projectId: 'project-rain-sword', projectTitle: '听雨不归人 · 第1集', status: 'running', progress: 68, progressSource: 'comfyui', comfyProgress: 58, progressTelemetryState: 'receiving', remoteState: 'running', currentStep: '生成镜头 7 / 12', createdAt: now, eta: '约 08:40', nodes: [
    { id: 'n1', label: '剧本拆解', type: 'script', status: 'completed', progress: 100 }, { id: 'n2', label: 'H3 提示词', type: 'prompt', status: 'completed', progress: 100 }, { id: 'n3', label: '一致性参考图', type: 'image', status: 'completed', progress: 100 }, { id: 'n4', label: '视频生成', type: 'video', status: 'running', progress: 58 }, { id: 'n5', label: '原生音频', type: 'audio', status: 'pending', progress: 0 }, { id: 'n6', label: '合成与导出', type: 'export', status: 'pending', progress: 0 }
  ] },
  { id: 'wf-002', projectId: 'project-neon-evidence', projectTitle: '零点证词 · 角色定妆', status: 'completed', progress: 100, progressSource: 'terminal', progressTelemetryState: 'connected', remoteState: 'succeeded', currentStep: '已完成', createdAt: '2026-08-07T12:30:00Z', nodes: [
    { id: 'm1', label: '角色设定', type: 'prompt', status: 'completed', progress: 100 }, { id: 'm2', label: '参考图生成', type: 'image', status: 'completed', progress: 100 }, { id: 'm3', label: '资产入库', type: 'compose', status: 'completed', progress: 100 }
  ] }
]

export const mockSettings: ComfySettings = {
  host: 'http://127.0.0.1:8188', installPath: 'C:\\path\\to\\ComfyUI_windows_portable', outputPath: 'C:\\path\\to\\ComfyUI_windows_portable\\ComfyUI\\output', workflowPath: '', concurrency: 1, autoStart: false, apiKey: ''
}
