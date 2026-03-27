import json, os

FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doupo.json")

with open(FILE_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

# =============================================
# MOD 1: system_prompt_supplement - currency
# =============================================
sps = data["system_prompt_supplement"]

old_cur = "\u8d27\u5e01\u4e3a**\u91d1\u5e01**\uff0c\u4e0d\u4f7f\u7528\u7075\u77f3\u3002state_update \u4e2d\u8bf7\u4f7f\u7528 `\u91d1\u5e01` \u5b57\u6bb5\u4ee3\u66ff `\u7075\u77f3`\u3002"
# Readable: 货币为**金币**，不使用灵石。state_update 中请使用 `金币` 字段代替 `灵石`。

new_cur = "\u8d27\u5e01\u5728\u53d9\u4e8b\u4e2d\u79f0\u4e3a**\u91d1\u5e01**\u3002\u4f46\u5728 state_update \u4e2d\uff0c\u8d27\u5e01\u5b57\u6bb5\u540d\u4ecd\u7136\u4f7f\u7528 `\u7075\u77f3`\uff08\u8fd9\u662f\u7cfb\u7edf\u5185\u90e8\u7edf\u4e00\u5b57\u6bb5\u540d\uff09\u3002\u53d9\u4e8b\u63cf\u5199\u65f6\u8bf7\u5c06\u201c\u7075\u77f3\u201d\u66ff\u6362\u4e3a\u201c\u91d1\u5e01\u201d\uff0c\u4f8b\u5982\uff1a\u201c\u4f60\u83b7\u5f97\u4e86100\u91d1\u5e01\u201d\uff0c\u4f46 JSON \u4e2d\u5199 `\"current_life.\u7075\u77f3\": 100`\u3002"
# Readable: 货币在叙事中称为**金币**。但在 state_update 中，货币字段名仍然使用 `灵石`（这是系统内部统一字段名）。叙事描写时请将"灵石"替换为"金币"，例如："你获得了100金币"，但 JSON 中写 `"current_life.灵石": 100`。

assert old_cur in sps, "MOD1 FAIL: old currency not found"
sps = sps.replace(old_cur, new_cur)
data["system_prompt_supplement"] = sps
print("[OK] MOD 1: currency")

# =============================================
# MOD 2a: start_prompt_template - rules
# =============================================
spt = data["start_prompt_template"]

old_2a = "\u7ee7\u627f\u5176\u8eab\u4efd\u3001\u5883\u754c\u3001\u6240\u5728\u5730\u3001\u4eba\u9645\u5173\u7cfb\n   - \u4ece\u8be5\u89d2\u8272\u6545\u4e8b\u65e9\u671f\u7684\u67d0\u4e2a\u5173\u952e\u65f6\u523b\u5f00\u59cb\uff08\u5982\u8427\u708e\u88ab\u6d4b\u51fa\u6597\u4e4b\u6c14\u4e09\u6bb5\u65f6\u3001\u7eb3\u5170\u5ae3\u7136\u9000\u5a5a\u524d\u7b49\uff09\n   - narrative\u4e2d\u8981\u63cf\u8ff0\u201c\u4f60\u7741\u5f00\u53cc\u773c\uff0c\u53d1\u73b0\u81ea\u5df1\u6210\u4e86xxx\uff0c\u539f\u4e3b\u7684\u8bb0\u5fc6\u5982\u6f6e\u6c34\u822c\u6d8c\u6765\u2026\u2026\u201d\n   - \u4fdd\u6301\u539f\u8457\u4e2d\u8be5\u89d2\u8272\u5df2\u6709\u7684\u4eba\u7269\u5173\u7cfb"
# Readable:
# 继承其身份、境界、所在地、人际关系
#    - 从该角色故事早期的某个关键时刻开始（如萧炎被测出斗之气三段时、纳兰嫣然退婚前等）
#    - narrative中要描述"你睁开双眼，发现自己成了xxx，原主的记忆如潮水般涌来……"
#    - 保持原著中该角色已有的人物关系

new_2a = (
    "\u7ee7\u627f\u5176\u8eab\u4efd\u3001\u5883\u754c\u3001\u6240\u5728\u5730\n"
    "   - **\u3010\u5f00\u5c40\u573a\u666f\u00b7\u4e25\u683c\u9075\u5b88\u3011** "
    "\u5fc5\u987b\u4ece\u8be5\u89d2\u8272\u9884\u8bbe\u7684\u300c\u5f00\u5c40\u573a\u666f\u300d"
    "\u5f00\u59cb\u53d9\u4e8b\uff08\u89c1\u4e0b\u65b9\u3010\u7cfb\u7edf\u9884\u8bbe\u3011"
    "\u90e8\u5206\uff09\u3002\u5982\u679c\u9884\u8bbe\u4e2d\u6709 `opening_scenario` "
    "\u5b57\u6bb5\uff0c\u4f60**\u5fc5\u987b**\u4e25\u683c\u6309\u7167\u5176\u63cf\u8ff0"
    "\u6765\u6784\u5efa\u5f00\u5c40\u53d9\u4e8b\u3002\n"
    "   - **\u3010\u5267\u60c5\u63a8\u8fdb\u00b7\u4e25\u7981\u8df3\u8dc3\u3011** "
    "\u5f00\u5c40\u65f6\u53ea\u5c55\u793a\u5f53\u524d\u573a\u666f\u4e2d\u5728\u573a\u7684NPC"
    "\uff0c\u4e0d\u53ef\u63d0\u524d\u5f15\u5165\u5c1a\u672a\u767b\u573a\u7684\u89d2\u8272"
    "\u3002\u4f8b\u5982\u8427\u708e\u5f00\u5c40\u65f6\u53ea\u5728\u8427\u5bb6\u6597\u6280"
    "\u573a\u6d4b\u8bd5\u6597\u6c14\uff0c\u6b64\u65f6\u4e0d\u5e94\u51fa\u73b0\u836f\u8001"
    "\u3001\u7eb3\u5170\u5ae3\u7136\u7b49\u540e\u7eed\u5267\u60c5\u624d\u767b\u573a\u7684"
    "\u89d2\u8272\u3002\u540e\u7eedNPC\u5e94\u968f\u7740\u73a9\u5bb6\u884c\u52a8\u548c"
    "\u5267\u60c5\u81ea\u7136\u63a8\u8fdb\u624d\u9010\u6b65\u51fa\u573a\u3002\n"
    "   - narrative\u4e2d\u8981\u63cf\u8ff0\u201c\u4f60\u7741\u5f00\u53cc\u773c\uff0c"
    "\u53d1\u73b0\u81ea\u5df1\u6210\u4e86xxx\uff0c\u539f\u4e3b\u7684\u8bb0\u5fc6\u5982"
    "\u6f6e\u6c34\u822c\u6d8c\u6765\u2026\u2026\u201d\n"
    "   - \u5f00\u5c40\u4eba\u7269\u5173\u7cfb\u53ea\u5305\u542b\u9884\u8bbe\u4e2d\u63d0"
    "\u4f9b\u7684NPC\uff08\u5373\u6b64\u523b\u5df2\u5b58\u5728\u4e8e\u89d2\u8272\u751f"
    "\u6d3b\u4e2d\u7684\u4eba\uff09\uff0c\u540e\u7eedNPC\u901a\u8fc7\u5267\u60c5\u89e6"
    "\u53d1\u52a0\u5165"
)

assert old_2a in spt, "MOD2a FAIL: old rules not found"
spt = spt.replace(old_2a, new_2a)
print("[OK] MOD 2a: rules")

# =============================================
# MOD 2b: coin field - 金币 -> 灵石
# =============================================
old_2b = "\u91d1\u5e01`(\u8bbe\u4e3a1)"
# Readable: 金币`(设为1)
new_2b = "\u7075\u77f3`(\u8bbe\u4e3a1\uff0c\u53d9\u4e8b\u4e2d\u79f0\u4e3a\u201c\u91d1\u5e01\u201d)"
# Readable: 灵石`(设为1，叙事中称为"金币")

assert old_2b in spt, "MOD2b FAIL: old coin not found"
spt = spt.replace(old_2b, new_2b)
print("[OK] MOD 2b: coin")

# =============================================
# MOD 2c: NPC instruction
# =============================================
old_2c = "\u4eba\u7269\u5173\u7cfb`\uff1a\u5982\u679c\u9b42\u7a7f\u4e86\u5df2\u77e5\u89d2\u8272\uff0c\u5fc5\u987b\u5305\u542b\u539f\u8457\u4e2d\u8be5\u89d2\u8272\u5df2\u6709\u7684\u5173\u952eNPC\u5173\u7cfb\uff08\u5982\u8427\u708e\u2192\u8427\u85b0\u513f\u3001\u8427\u6218\u3001\u836f\u8001\u7b49\uff09\u3002\u6bcf\u4e2aNPC\u9700\u5305\u542b\u5883\u754c\uff08\u6597\u6c14\u4f53\u7cfb\uff09\u3001\u529f\u6cd5\u7b49\u3002"
# Readable: 人物关系`：如果魂穿了已知角色，必须包含原著中该角色已有的关键NPC关系（如萧炎→萧薰儿、萧战、药老等）。每个NPC需包含境界（斗气体系）、功法等。

new_2c = "\u4eba\u7269\u5173\u7cfb`\uff1a**\u7cfb\u7edf\u4f1a\u81ea\u52a8\u6ce8\u5165\u9884\u8bbe\u89d2\u8272\u7684\u521d\u59cb\u4eba\u7269\u5173\u7cfb\uff08\u542b\u597d\u611f\u5ea6\u7b49\u6570\u636e\uff09\uff0c\u4f60\u53ea\u9700\u5728\u53d9\u4e8b\u4e2d\u81ea\u7136\u63d0\u53ca\u5728\u573a\u7684NPC\u5373\u53ef\u3002** \u5982\u679c\u662f\u975e\u9884\u8bbe\u89d2\u8272\uff0c\u8bf7\u81ea\u884c\u751f\u6210\u5408\u7406\u7684\u521d\u59cbNPC\u5173\u7cfb\u3002"
# Readable: 人物关系`：**系统会自动注入预设角色的初始人物关系（含好感度等数据），你只需在叙事中自然提及在场的NPC即可。** 如果是非预设角色，请自行生成合理的初始NPC关系。

assert old_2c in spt, "MOD2c FAIL: old NPC not found"
spt = spt.replace(old_2c, new_2c)
data["start_prompt_template"] = spt
print("[OK] MOD 2c: NPC instruction")

# =============================================
# MOD 3: Add opening_scenario to xiaoyan
# =============================================
xiaoyan = data["character_presets"]["\u8427\u708e"]

opening = "\u9b42\u7a7f\u65f6\u523b\uff1a\u8427\u5bb6\u5e74\u5ea6\u6597\u6c14\u6d4b\u9a8c\u65e5\u3002\u8427\u708e\u7ad9\u5728\u8427\u5bb6\u6597\u6280\u573a\u7684\u6d4b\u9a8c\u77f3\u7891\u524d\uff0c\u5468\u56f4\u662f\u8427\u5bb6\u65cf\u4eba\u56f4\u89c2\u7684\u76ee\u5149\u3002\u4e09\u5e74\u524d\u4ed6\u8fd8\u662f\u8427\u5bb6\u5929\u624d\uff0c\u5982\u4eca\u5374\u88ab\u6d4b\u51fa\u4ec5\u6709\u6597\u4e4b\u6c14\u4e09\u6bb5\uff0c\u6ca6\u4e3a\u5168\u57ce\u7b11\u67c4\u3002\u5929\u9053\u5f15\u5bfc\u73a9\u5bb6\u63a5\u53d7\u8fd9\u4e2a\u8eab\u4efd\uff0c\u611f\u53d7\u5468\u56f4\u4eba\u7684\u5632\u8bbd\u4e0e\u5931\u671b\u3002\u3010\u6ce8\u610f\u3011\u6b64\u65f6\u836f\u8001\u5c1a\u672a\u82cf\u9192\uff08\u4ed6\u85cf\u5728\u8427\u708e\u624b\u4e0a\u7684\u9ed1\u8272\u6212\u6307\u4e2d\uff0c\u8981\u5230\u540e\u7eed\u8427\u708e\u53bb\u540e\u5c71\u6563\u5fc3\u65f6\u624d\u4f1a\u4e3b\u52a8\u73b0\u8eab\uff09\u3002\u7eb3\u5170\u5ae3\u7136\u4e5f\u5c1a\u672a\u4e0a\u95e8\u9000\u5a5a\uff08\u9000\u5a5a\u4e8b\u4ef6\u53d1\u751f\u5728\u540e\u7eed\u5267\u60c5\u4e2d\uff09\u3002\u5f00\u5c40\u53ea\u5e94\u51fa\u73b0\u8427\u85b0\u513f\u3001\u8427\u6218\u3001\u8427\u5a9a\u3001\u8427\u5b81\u7b49\u8427\u5bb6\u4e2d\u4eba\u3002"

new_xy = {}
for k, v in xiaoyan.items():
    new_xy[k] = v
    if k == "\u521d\u59cb\u5883\u754c":
        new_xy["opening_scenario"] = opening
data["character_presets"]["\u8427\u708e"] = new_xy
assert "opening_scenario" in data["character_presets"]["\u8427\u708e"]
print("[OK] MOD 3: opening_scenario")

# =============================================
# WRITE
# =============================================
out = json.dumps(data, ensure_ascii=False, indent=2)
with open(FILE_PATH, "w", encoding="utf-8") as f:
    f.write(out)

# =============================================
# VALIDATE
# =============================================
with open(FILE_PATH, "r", encoding="utf-8") as f:
    v = json.load(f)

# Mod 1
assert "\u53d9\u4e8b\u4e2d\u79f0\u4e3a**\u91d1\u5e01**" in v["system_prompt_supplement"], "V1 fail"
assert "\u4e0d\u4f7f\u7528\u7075\u77f3" not in v["system_prompt_supplement"], "V1 old still present"

# Mod 2a
assert "\u5f00\u5c40\u573a\u666f\u00b7\u4e25\u683c\u9075\u5b88" in v["start_prompt_template"], "V2a fail"
assert "\u4eba\u9645\u5173\u7cfb" not in v["start_prompt_template"], "V2a old still present"

# Mod 2b
assert "\u7075\u77f3`(\u8bbe\u4e3a1" in v["start_prompt_template"], "V2b fail"
assert "\u91d1\u5e01`(\u8bbe\u4e3a1)" not in v["start_prompt_template"], "V2b old still present"

# Mod 2c
assert "\u7cfb\u7edf\u4f1a\u81ea\u52a8\u6ce8\u5165" in v["start_prompt_template"], "V2c fail"

# Mod 3
assert "opening_scenario" in v["character_presets"]["\u8427\u708e"], "V3 fail"
keys = list(v["character_presets"]["\u8427\u708e"].keys())
assert keys.index("opening_scenario") == keys.index("\u521d\u59cb\u5883\u754c") + 1, "V3 order fail"

print("\n=== ALL 4 MODIFICATIONS APPLIED AND VERIFIED ===")
print("Xiaoyan keys:", keys)
