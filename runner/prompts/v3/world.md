根据题目设计开局要用的世界。只输出 JSON：

{{
  "name": "短名",
  "premise": "一句前提",
  "rules": ["这套世界怎么运转"],
  "institutions": [
    {{
      "name": "名称",
      "wants": "要什么",
      "can": "能做什么",
      "cannot": "不能或不做的"
    }}
  ],
  "opening_constraints": ["开局里谁能做什么、不能做什么"],
  "taboos": ["你自己立的禁区"],
  "unresolved": ["开篇不必写死的"]
}}

题目：

{direction}
