根据书籍定位和卷级总纲，为开篇约 5 万字拟定逐章细纲。只输出以下 JSON：

{{
  "target_total_chars": 52000,
  "macro_scope": "开篇范围、起止状态和暂不解决的全书冲突",
  "chapters": [
    {{
      "number": 1,
      "title": "章节名",
      "target_chars": 3200,
      "summary": "剧情摘要",
      "beats": ["3–5 个按顺序发生的主要场景"],
      "continuity_in": ["承接状态"],
      "continuity_out": ["章末状态"],
      "foreshadowing": ["本章涉及的伏笔"]
    }}
  ]
}}

要求：
1. 共 16–18 章，number 连续。
2. target_total_chars 及各章 target_chars 总和均不少于 48000，两者相差不超过 500；每章正文通常约 3000–4000 个可计字符，不设硬上限。
3. 每章列 3–5 个主要场景，写清行动、阻力、选择和结果；continuity_in/out 前后衔接。
4. 开篇结尾形成第一次不可逆越界，不提前解决全书核心冲突。
5. 只输出 JSON，不加代码围栏。
