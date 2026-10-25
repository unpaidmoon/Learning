"""Small task-specific structured output contracts for the native answering model."""


def response_contract(
    kind: str, *, answer_review: dict | None = None, essay_review: dict | None = None
) -> str:
    common = '"block_id":"part_1","markdown":"正文或题干或卡片正面","attribution":"source_paraphrase","source_snapshot_ids":["本次实际snapshot_id"]'
    if kind == "research":
        return (
            '仅返回严格JSON对象，不加围栏：{"blocks":[{' + common + ',"kind":"markdown"}]}。'
            "报告按研究主题写可读对照与证据说明，保留事实出处、分歧及未解决问题。"
            "表格每项说清比较维度与来源。资料直接陈述和忠实归纳用source_paraphrase；基于实际资料的对照分析、局限判断或推断用source_inference，"
            "也必须引用实际快照，在正文明确依据、假设或不确定性，不冒充原文。只依据资料时禁止ai_supplement及外部事实。"
            "本任务只写研究报告，不新编练习、卡片、扩展示例或无证据的新知识。不要为凑章节添加无关内容。"
            "正文直接讨论研究内容，用资料标题或短称指代来源；快照ID、leaf_id、字段名、字符区间、调度和运行说明不写入正文。"
            "模型不能决定接受部分范围、修改用户来源或替用户记录学习行为。"
        )
    if kind == "essay_review":
        if essay_review is None:
            raise ValueError("essay review requires its frozen draft and round")
        import json

        target = json.dumps(
            {
                key: essay_review[key]
                for key in ("round_id", "draft_revision_id", "draft_content_hash")
            },
            ensure_ascii=False,
        )
        return (
            '仅返回严格JSON对象，不加围栏，只能有一个essay_feedback块：{"blocks":[{'
            + common
            + ',"kind":"essay_feedback","feedback":'
            + target[:-1]
            + ',"annotated_text":"实际作文原文，可加入下述批注标签"}}]}。'
            'markdown必须包含完整评语、修改理由以及恰好一个完整<score total="参考总分" max="本稿满分">标签；'
            '每个评分维度使用<dim name="与rubric完全一致的名称" score="该维度分数" max="该维度满分">理由</dim>，最后</score>闭合。'
            "总分和维度不得超过各自满分，sum类型各维度之和须等于总分；无维度时不虚构维度。"
            'annotated_text保留实际稿原文，可用<err type="grammar" explanation="理由">实际错误原文</err>标注，'
            '或<replace old="实际待改文字" new="建议替换" reason="理由"/>、<del reason="理由">待删原文</del>、<ins>新增文字</ins>。'
            "凡已能给出明确替换的语法、拼写或表达建议，必须在annotated_text中用replace的old/new实际编码；不能只写在正文表格或err解释里。"
            "err只用于尚无可靠替换文本的纯批注，不从解释文字猜测或自动采纳替换。没有错误时可保留纯原文。"
            "相同短语须保留足够上下文，不能把批注定位到其他重复位置。不要trim、改写或冒充用户已采纳，批注和分数都是AI参考。"
            "严格按输入topic/rubric评估，参考标准不足时在评语中说明，不声称正式考试成绩。"
            "rubric未给出分项权重时，不虚构逐类扣分或修改后能回收多少分；整体参考分只说明实际依据及限制。"
        )
    if kind == "answer_review":
        if answer_review is None:
            raise ValueError("answer review requires its frozen business target")
        import json

        target = json.dumps(
            {
                "answer_id": answer_review["answer_id"],
                "expected_revision": answer_review["expected_revision"],
                "verdict": "pending",
                "score": None,
            },
            ensure_ascii=False,
        )
        return (
            '仅返回严格JSON对象，不加围栏，只能有一个assessment块：{"blocks":[{'
            + common
            + ',"kind":"assessment","assessment":'
            + target
            + "}]}。"
            "markdown写针对实际作答的参考判分理由、具体问题和依据；不是替用户重新作答。"
            "verdict可为correct/wrong/partial/pending；无法可靠判断或条件不足必须pending，pending的score为null。"
            "其他score可用0到1的参考比例或null，不能当校准概率。"
            "AI参考答案本身不证明正确，须核实际题意和实际作答；两份输入都要引用。"
            "这是AI参考评价，用户可改判；不写用户自评或掌握结论。"
        )
    if kind == "book":
        return (
            '仅返回严格JSON对象，不加围栏，书页格式{"blocks":[{'
            + common
            + ',"kind":"markdown"}]}。'
            "同一blocks可混排正文markdown、练习question、受限图形visualization；每块用唯一block_id，保留实际来源与AI补充身份。"
            "依据本章目标、用户要求和本页实际材料组织连贯内容，可以重组讲解，但不能声称读完未提供的后续材料。"
            '题目示例：{"block_id":"exercise_1","kind":"question","markdown":"题干","attribution":"source_paraphrase",'
            '"source_snapshot_ids":["实际snapshot_id"],"question":{"question_type":"short_answer","options":[],"reference_answer":"AI参考答案","explanation":"依据与解析","answer_authority":"ai_reference"}}。'
            "题目类型可single_choice/multiple_choice/true_false/short_answer/calculation/proof等；有选项时options是option_id/text对象数组。"
            "option_id必须是单个大写英文字母A到Z，例如A、B；判断题也可省略options，不使用true/false或option_a作为选项标识。"
            "不代替用户作答，不生成成绩、掌握、复习卡片或目录确认。材料适合时提供一两个可独立作答的练习。"
            "按资料新编的练习必须在题干明确标注‘新编练习’，不能称教材原题或偷换原例条件；"
            "question块的source_paraphrase仅标记资料依据，题干和AI参考答案并非原文引述。"
            '需要互动函数图时，visualization块除公共字段外附"visualization":{"render_type":"cartesian","renderer":{"native_renderer":"cartesian"},'
            '"presentation":{"title":"图形标题","alt_text":"文字说明"},"payload":{"data":{"kind":"cartesian","expression":"a*x^2",'
            '"parameters":[{"symbol":"a","label":"系数a","min":-2,"max":2,"step":0.1,"initial":1}],"x_range":{"min":-3,"max":3},'
            '"y_range":{"min":-20,"max":20},"probe_x":1,"animate_parameter":null}}}。'
            "函数只用有限数值、x、已声明参数、pi/e、四则、^和sin/cos/tan/exp/log/sqrt/abs；动画可指定一个已有参数。"
            "不生成expression_tree、HTML、SVG源码、iframe、脚本、URL、远端ref、事件桥或任意代码；无合适图形时用文字解释，不硬造图。"
        )
    if kind == "visualization":
        return (
            '仅返回严格JSON对象，不加围栏。图形必须用visualization块：{"blocks":[{'
            + common
            + ',"kind":"visualization","visualization":{"render_type":"cartesian",'
            '"renderer":{"native_renderer":"cartesian"},"presentation":{"title":"函数与参数","description":"说明变量、范围和限制","alt_text":"图形的文字说明"},'
            '"payload":{"data":{"kind":"cartesian","expression":"a*x^2","parameters":[{"symbol":"a","label":"系数a","min":-2,"max":2,"step":0.1,"initial":1}],'
            '"x_range":{"min":-4,"max":4},"y_range":{"min":-10,"max":10},"probe_x":1,"sample_count":256,"animate_parameter":"a","duration_seconds":8}}}}]}。'
            "只生成封闭的图形数据，不写HTML、SVG源码、JavaScript、iframe、URL、外链资源、文件路径、事件桥、回调、schema引用或fallback。"
            "cartesian是y=f(x)的平面函数图；expression只允许有限数值、x、已声明参数、pi/e、+ - * / ^、括号和单参数sin/cos/tan/exp/log/sqrt/abs。"
            "不输出expression_tree，程序自行编译校验；参数最多8个，须确实参与函数，范围和步长合理，probe_x在横轴范围内。"
            "静态函数图可parameters为空，animate_parameter为null；参数互动明确参数意义；动画指定一个已有参数，只有用户点播放才开始。"
            "若需要关系／几何示意，用render_type及native_renderer均为diagram，payload.data为"
            '{"kind":"diagram","nodes":[{"node_id":"a","x":20,"y":50,"label":"节点A","shape":"circle","size":4,"color":"blue"},{"node_id":"b","x":80,"y":50,"label":"节点B","shape":"box","size":4,"color":"green"}],'
            '"edges":[{"source":"a","target":"b","label":"关系","arrow":true}]}。'
            "节点坐标是0到100的画布百分比，不把排版距离冒充物理比例；shape可point/circle/box，color可blue/orange/green/gray/red。"
            "一幅图保留对应解释和实际来源，数值、公式、单位与资料相符。来源无依据的推导分为ai_supplement；不能把生成的图说成教材原图。"
        )
    if kind == "learning_path":
        return (
            '仅返回严格JSON对象，不加围栏。每个知识点是一个path_step块：{"blocks":[{'
            + common
            + ',"kind":"path_step","step":{"title":"知识点名称","objective":"学习目标",'
            '"suggested_order":0,"recall_prompt":"不看解析时的回忆问题",'
            '"explanation":"回忆后可查看的解释","questions":[]}}]}。'
            "按目标给建议顺序，用户可随时跳过、返回、查看完整解释，不设置闯关条件。"
            "每个知识点保留实际来源；这份路线不包含用户作答、自评、成绩、已掌握或进度。"
            "questions默认为空，只能链接本次实际输入中的结构化题目，链接严格含artifact_id/revision_id/block_id，"
            "不从资料中的题号猜测任何身份，不虚构题目链接。建议练习可写在markdown中，不伪造正式作答。"
        )
    if kind == "question_set":
        return (
            '仅返回严格JSON对象，不加围栏。题目必须是结构化question块，格式：{"blocks":[{'
            + common
            + ',"kind":"question","question":{"question_type":"single_choice",'
            '"options":[{"option_id":"A","text":"选项一"},{"option_id":"B","text":"选项二"}],'
            '"reference_answer":"A","explanation":"依据及解析","answer_authority":"ai_reference"}}]}。'
            "question_type可用single_choice/multiple_choice/true_false/short_answer/essay/calculation/proof/numeric/ordering/matching；"
            "主观题options为空数组。每题保留来源，不生成任何用户作答或用户评分。你的答案都是ai_reference，不能标成user_confirmed；无法给答案用missing和空reference_answer。"
        )
    if kind == "card_set":
        return (
            '仅返回严格JSON对象，不加围栏。复习卡片必须是结构化card块，格式：{"blocks":[{'
            + common
            + ',"kind":"card","card":{"back":"背面答案与必要解释"}}]}。'
            "一张卡只问一个可回忆的要点，每张卡保留实际来源；不生成用户翻面、评分或掌握记录。"
        )
    return '仅返回严格JSON对象，不加围栏，格式为{"blocks":[{' + common + ',"kind":"markdown"}]}。'
