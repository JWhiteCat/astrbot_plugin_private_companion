"""Conservative, model-free public-help references, adapted from V1."""

import re

from .group_observation import parse_moment
from .group_timing import clean_group_text


QUESTION = re.compile(r"[?？]|怎么|怎样|如何|为什么|为啥|咋办|怎么办|有没有|谁懂|谁知道|"
                      r"求问|求助|求教|请教|帮帮|帮忙|帮我|能不能|可以吗|是否|吗(?:$|[，,。！!\s])|"
                      r"\b(?:how|why|help)\b", re.I)
PUBLIC_HELP = re.compile(r"(?:有没有人|有人|谁|哪位|大佬).{0,14}(?:懂|知道|会|能帮|帮忙)|"
                         r"求问|求助|求教|请教|急求|急问|在线等|帮忙|帮我|怎么办|咋办|"
                         r"怎么(?:弄|解决|处理|修复)|如何(?:处理|修复|解决)", re.I)
TROUBLE = re.compile(r"报错|异常|error|traceback|\bbug\b|闪退|崩溃|跑不起来|卡住|"
                     r"(?:安装|配置|运行|启动|登录|请求|连接|编译|构建|部署|调用|加载|插件|程序).{0,8}失败", re.I)
URGENT = re.compile(r"急求|急问|急用|在线等|有点急|很急|急着", re.I)
SHORT_REACTION = re.compile(r"^(?:啊|诶|欸|哈|草|不是|不会吧|真的假的|所以|那|这|啥|"
                            r"为什么|为啥|怎么|咋|什么情况|啥情况)[，,。.\s]*[^，,。！？!?]{0,24}[?？]?$", re.I)


def legacy_cold_group_score(text):
    """Preserve V1 cold-group lexical scores without inventing state evidence.

    Args:
        text: Visible, recipient-checked group message text.

    Returns:
        The original opening, greeting, or help score; zero for other text.
        Sleep and energy eligibility remain separate runtime state concerns.
    """
    cleaned = " ".join(str(text or "").split())[:260]
    if not 3 <= len(cleaned) <= 120:
        return 0
    if re.search(r"^(?:[/!！#]|签到|打卡|菜单|帮助|help\b)", cleaned, flags=re.I):
        return 0
    if re.search(
        r"(https?://|www\.|```|\[图片\]|\[语音\]|\[视频\]|\[转发消息\])",
        cleaned,
        flags=re.I,
    ):
        return 0
    if re.search(
        r"(有人吗|在吗|还在吗|睡了吗|怎么没人|好安静|冷群|冒泡|路过|诈尸|开门|醒醒)",
        cleaned,
    ):
        return 86
    if re.search(
        r"(早|早上好|上午好|中午好|下午好|晚上好|晚好|嗨|hello|hi|哈喽|冒个泡|有人不|人呢)",
        cleaned,
        flags=re.I,
    ):
        return 72
    if re.search(r"(急急如律令|急急国王|急急急急|急死谁了)", cleaned):
        return 0
    failure = (
        r"(安装|配置|运行|启动|登录|请求|发送|上传|下载|连接|编译|构建|部署|调用|识别|读取|解析|保存|加载|同步|执行|支付|打开|导入|导出|更新|提交|注册|验证|接口|插件|程序|脚本|命令|模型|图片|语音|视频|文件|消息|任务).{0,8}失败"
        r"|(?:一直|总是|老是|反复|还是|又).{0,6}失败|失败.{0,8}(怎么办|咋办|怎么弄|怎么解决|怎么处理|原因|报错|日志|重试|一直|总是|还是|又)"
    )
    if "救命" in cleaned and not (
        re.search(
            r"(怎么|咋办|怎么办|怎么弄|怎么解决|怎么处理|帮忙|帮我|报错|异常|卡住|跑不起来|急求|急问|在线等)",
            cleaned,
            flags=re.I,
        )
        or re.search(failure, cleaned, flags=re.I)
    ):
        return 0
    if re.search(
        r"(救命.{0,12}(怎么|咋办|怎么办|怎么弄|怎么解决|怎么处理|帮忙|帮我|报错|异常|卡住|跑不起来)|急求|急问|急用|急等|在线等|有点急|很急|比较急|挺急|急着|求问|请教|有没有人|有人懂|谁会|帮忙|咋办|怎么办|怎么弄|报错|崩了|卡住)",
        cleaned,
        flags=re.I,
    ) or re.search(failure, cleaned, flags=re.I):
        return 78
    return 0


def question_signal(text, *, recent=(), at=None, speaker_id="", bot_turns=(), cooldown_seconds=90):
    """Score an unsolicited question; explicit calls are admitted separately.

    Scores are candidate references, not a claim about intent. Cooldown and
    conversation cues reduce weak candidates; urgent public help can still
    pass. The role/Laya decides whether an admitted candidate deserves speech.
    """
    body = clean_group_text(text)
    notes = []
    if len(body) < 4 or not QUESTION.search(body) or "```" in body:
        return {"score": 0, "public_help": False, "notes": ["缺少实质问题或求助"]}
    public = bool(PUBLIC_HELP.search(body))
    trouble = bool(TROUBLE.search(body))
    urgent = bool(URGENT.search(body)) and (public or trouble)
    score = 0
    if public:
        score = 80
        notes.append("向群里明确求助")
    elif re.search(r"如何|怎样|怎么(?:做|用|安装|配置)|\bhow\b", body, re.I):
        score = 72
        notes.append("操作问题")
    elif re.search(r"为什么|为啥|怎么回事|什么情况|什么意思|啥意思|\bwhy\b", body, re.I):
        score = 68
        notes.append("原因或含义问题")
    elif re.search(r"值不值得|值得.*吗|靠谱不|靠谱吗|推荐|好用吗|怎么样|这是什么|有没有.{2,}", body):
        score = 68
        notes.append("识别或选择问题")
    elif re.search(r"怎么|什么|哪|谁|能不能|可以吗|是否|\bhelp\b", body, re.I):
        score = 54
        notes.append("一般疑问线索")
    if score == 0:
        return {"score": 0, "public_help": public, "notes": ["问号或语气词不足以唤醒"]}
    if trouble:
        score += 12
        notes.append("有具体排障线索")
    if urgent:
        score += 18
        notes.append("紧急实质求助")
    if not public and not trouble:
        if SHORT_REACTION.search(body) or re.search(r"(?:哈哈|笑死|绷不住|不是吧|不会吧).{0,8}[?？]?$", body):
            score -= 20
            notes.append("短反问或吐槽降权")
        if re.search(r"(?:你俩|他|她|楼上|上面|群主|管理员|作者).{0,16}[?？]$", body):
            score -= 22
            notes.append("问题目标更像其他成员")
    if at is not None:
        nearby = [item for item in recent
                  if (when := parse_moment(item.get("received_at"))) is not None
                  and 0 <= (at - when).total_seconds() <= 90]
        others = [item for item in nearby if item.get("speaker_id") != speaker_id]
        if not public and not trouble and others:
            score -= 8
            notes.append("其他成员正在交流")
            if any(re.search(r"因为|原因|解决|试试|改成|换成|应该|可以", item.get("text", ""))
                   for item in others[-3:]):
                score -= 8
                notes.append("附近已有讨论或回答")
        returned = [when for turn in bot_turns
                    if turn.get("reason") != "repeat" and turn.get("send_state") == "returned"
                    and (when := parse_moment(turn.get("returned_at"))) is not None and when <= at]
        if returned and cooldown_seconds > 0 and (at - max(returned)).total_seconds() < cooldown_seconds:
            penalty = 0 if urgent else 8 if public else 20
            score -= penalty
            if penalty:
                notes.append("近期已接话，答疑冷却降权")
    return {"score": min(100, max(0, score)), "public_help": public, "notes": notes}
