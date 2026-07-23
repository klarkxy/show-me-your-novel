在已经确认的书籍定位基础上，制定全书卷级总纲。只输出以下结构的 JSON 对象，不得增加或删除顶层字段：

{{
  "target_total_chars": 2000000,
  "volumes": [
    {{
      "number": 1,
      "title": "卷名",
      "target_chars": 150000,
      "period": "本卷覆盖的明确时间范围",
      "start_state": "卷首人物、关系、金钱、风险与信息状态",
      "end_state": "卷末发生且不可忽略的状态变化",
      "main_conflict": "贯穿本卷并在卷末发生转折的主要冲突",
      "arcs": [
        {{
          "title": "情节弧名称",
          "summary": "该情节弧的起因、升级、关键选择与阶段结果"
        }}
      ]
    }}
  ],
  "character_arcs": ["主要人物贯穿全书的起点、关键转折和终点"],
  "foreshadowing": ["重要伏笔的埋设卷、发展卷与兑现卷"],
  "ending": "最终不可逆行动、直接后果和仍未揭晓的公平悬念"
}}

要求：
1. volumes 必须包含 10–20 项，number 从 1 连续递增；每卷 arcs 必须包含 3–6 项。
2. 全书目标约 200 万个中文字符；各卷 target_chars 合计必须在 1800000–2200000 之间，并与 target_total_chars 相差不超过 10000。
3. period 必须构成前后连续的时间线；start_state 与上一卷 end_state 衔接。
4. character_arcs 和 foreshadowing 必须写出跨卷位置，不能只写抽象主题。
5. 开篇约五万字只展开第一卷的早期部分，不能提前完成全书 ending。
6. 只输出 JSON。

再次核对创作方向：

{direction}
