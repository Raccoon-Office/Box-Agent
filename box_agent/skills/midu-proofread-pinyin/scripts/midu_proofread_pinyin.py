#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Midu 拼音校对工具

调用接口对纯文本进行拼音校对，展示校对状态和结果链接，不自动下载服务端返回的 URL。

输入支持：纯文本（直接传入），支持"拼音行+汉字行"交替格式，会自动解析并组装为"汉字+拼音"格式后再校对。
本工具不处理文件读取，调用方（模型）需负责从 Word、PDF 等文件中提取纯文本后再传入。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

from dataclasses import dataclass

import requests


_SKILLS_ROOT = Path(__file__).resolve().parents[2]
if str(_SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILLS_ROOT))

from _midu_shared.auth_hint import build_auth_hint  # noqa: E402
from _midu_shared.midu_auth import load_credentials  # noqa: E402
from _midu_shared.service_errors import (  # noqa: E402
    RECHARGE_HINT,
    looks_like_auth_failure,
    looks_like_no_entitlement,
)


API_URL = "https://api.midu.com/ability/skill/jdt/proof/pinyin"
SKILL_CODE = "JDT_PROOF"

# 缺凭证 / 鉴权失效时统一引导走短信授权，禁止再引导去官网取 Key
AUTH_HINT = build_auth_hint()

# ============ 拼音解析/组装模块 ============

# 所有有效的拼音音节（不带声调）
VALID_PINYINS = {
    'a', 'ai', 'an', 'ang', 'ao',
    'ba', 'bai', 'ban', 'bang', 'bao', 'bei', 'ben', 'beng', 'bi', 'bian', 'biao', 'bie', 'bin', 'bing', 'bo', 'bu',
    'ca', 'cai', 'can', 'cang', 'cao', 'ce', 'cen', 'ceng', 'cha', 'chai', 'chan', 'chang', 'chao', 'che', 'chen', 'cheng', 'chi', 'chong', 'chou', 'chu', 'chua', 'chuai', 'chuan', 'chuang', 'chui', 'chun', 'chuo', 'ci', 'cong', 'cou', 'cu', 'cuan', 'cui', 'cun', 'cuo',
    'da', 'dai', 'dan', 'dang', 'dao', 'de', 'dei', 'deng', 'di', 'dia', 'dian', 'diao', 'die', 'ding', 'diu', 'dong', 'dou', 'du', 'duan', 'dui', 'dun', 'duo',
    'e', 'ei', 'en', 'eng', 'er',
    'fa', 'fan', 'fang', 'fei', 'fen', 'feng', 'fo', 'fou', 'fu',
    'ga', 'gai', 'gan', 'gang', 'gao', 'ge', 'gei', 'gen', 'geng', 'gong', 'gou', 'gu', 'gua', 'guai', 'guan', 'guang', 'gui', 'gun', 'guo',
    'ha', 'hai', 'han', 'hang', 'hao', 'he', 'hei', 'hen', 'heng', 'hong', 'hou', 'hu', 'hua', 'huai', 'huan', 'huang', 'hui', 'hun', 'huo',
    'ji', 'jia', 'jian', 'jiang', 'jiao', 'jie', 'jin', 'jing', 'jiong', 'jiu', 'ju', 'juan', 'jue', 'jun',
    'ka', 'kai', 'kan', 'kang', 'kao', 'ke', 'ken', 'keng', 'kong', 'kou', 'ku', 'kua', 'kuai', 'kuan', 'kuang', 'kui', 'kun', 'kuo',
    'la', 'lai', 'lan', 'lang', 'lao', 'le', 'lei', 'leng', 'li', 'lia', 'lian', 'liang', 'liao', 'lie', 'lin', 'ling', 'liu', 'long', 'lou', 'lu', 'luan', 'lue', 'lun', 'luo', 'lv',
    'ma', 'mai', 'man', 'mang', 'mao', 'me', 'mei', 'men', 'meng', 'mi', 'mian', 'miao', 'mie', 'min', 'ming', 'miu', 'mo', 'mou', 'mu',
    'na', 'nai', 'nan', 'nang', 'nao', 'ne', 'nei', 'nen', 'neng', 'ni', 'nian', 'niang', 'niao', 'nie', 'nin', 'ning', 'niu', 'nong', 'nou', 'nu', 'nuan', 'nue', 'nun', 'nuo', 'nv',
    'o', 'ou',
    'pa', 'pai', 'pan', 'pang', 'pao', 'pei', 'pen', 'peng', 'pi', 'pian', 'piao', 'pie', 'pin', 'ping', 'po', 'pou', 'pu',
    'qi', 'qia', 'qian', 'qiang', 'qiao', 'qie', 'qin', 'qing', 'qiong', 'qiu', 'qu', 'quan', 'que', 'qun',
    'ran', 'rang', 'rao', 're', 'ren', 'reng', 'ri', 'rong', 'rou', 'ru', 'ruan', 'rui', 'run', 'ruo',
    'sa', 'sai', 'san', 'sang', 'sao', 'se', 'sen', 'seng', 'sha', 'shai', 'shan', 'shang', 'shao', 'she', 'shei', 'shen', 'sheng', 'shi', 'shou', 'shu', 'shua', 'shuai', 'shuan', 'shuang', 'shui', 'shun', 'shuo', 'si', 'song', 'sou', 'su', 'suan', 'sui', 'sun', 'suo',
    'ta', 'tai', 'tan', 'tang', 'tao', 'te', 'tei', 'teng', 'ti', 'tian', 'tiao', 'tie', 'ting', 'tong', 'tou', 'tu', 'tuan', 'tui', 'tun', 'tuo',
    'wa', 'wai', 'wan', 'wang', 'wei', 'wen', 'weng', 'wo', 'wu',
    'xi', 'xia', 'xian', 'xiang', 'xiao', 'xie', 'xin', 'xing', 'xiong', 'xiu', 'xu', 'xuan', 'xue', 'xun',
    'ya', 'yan', 'yang', 'yao', 'ye', 'yi', 'yin', 'ying', 'yo', 'yong', 'you', 'yu', 'yuan', 'yue', 'yun',
    'za', 'zai', 'zan', 'zang', 'zao', 'ze', 'zei', 'zen', 'zeng', 'zha', 'zhai', 'zhan', 'zhang', 'zhao', 'zhe', 'zhei', 'zhen', 'zheng', 'zhi', 'zhong', 'zhou', 'zhu', 'zhua', 'zhuai', 'zhuan', 'zhuang', 'zhui', 'zhun', 'zhuo', 'zi', 'zong', 'zou', 'zu', 'zuan', 'zui', 'zun', 'zuo'
}

# 声调字符映射（特殊字母+带声调字符 -> 无声调基础字符）
TONE_MAP = {
    'ā': 'a', 'á': 'a', 'ǎ': 'a', 'à': 'a',
    'ē': 'e', 'é': 'e', 'ě': 'e', 'è': 'e',
    'ī': 'i', 'í': 'i', 'ǐ': 'i', 'ì': 'i',
    'ō': 'o', 'ó': 'o', 'ǒ': 'o', 'ò': 'o',
    'ū': 'u', 'ú': 'u', 'ǔ': 'u', 'ù': 'u',
    'ǖ': 'ü', 'ǘ': 'ü', 'ǚ': 'ü', 'ǜ': 'ü',
    'ń': 'n', 'ň': 'n', 'ǹ': 'n',
    'ḿ': 'm',
    'ɡ': 'g',  # 特殊拉丁字母 ɡ -> g
    'ɑ': 'a',  # 特殊拉丁字母 ɑ -> a
}

# 允许出现在拼音中的字符集合
_PINYIN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜńňǹḿɡɑ"
)


def remove_tone(pinyin: str) -> str:
    """去除拼音中的声调，返回小写无调拼音"""
    result = []
    for ch in pinyin:
        if ch in TONE_MAP:
            result.append(TONE_MAP[ch])
        else:
            result.append(ch.lower())
    return ''.join(result)


def split_pinyin(pinyin_str: str, hanzi_count: int) -> List[str]:
    """
    将连续的拼音字符串切分成指定数量的拼音音节。
    使用基于汉字数量的回溯+记忆化，确保切分出的拼音数与汉字数一致。
    """
    pinyin_str = pinyin_str.strip()
    if not pinyin_str:
        return []

    n = len(pinyin_str)
    memo = {}

    def dfs(start: int, remaining: int) -> Optional[List[str]]:
        if remaining == 0:
            return [] if start == n else None
        if start >= n:
            return None
        if (start, remaining) in memo:
            return memo[(start, remaining)]

        for end in range(min(start + 7, n + 1), start, -1):
            substr = pinyin_str[start:end]
            substr_no_tone = remove_tone(substr)
            if substr_no_tone in VALID_PINYINS:
                result = dfs(end, remaining - 1)
                if result is not None:
                    memo[(start, remaining)] = [substr] + result
                    return memo[(start, remaining)]

        memo[(start, remaining)] = None
        return None

    result = dfs(0, hanzi_count)
    if result is not None:
        return result

    # fallback：退化为贪心切分
    result = []
    i = 0
    while i < n:
        found = False
        for j in range(min(i + 7, n), i, -1):
            substr = pinyin_str[i:j]
            if remove_tone(substr) in VALID_PINYINS:
                result.append(substr)
                i = j
                found = True
                break
        if not found:
            result.append(pinyin_str[i])
            i += 1

    # 修正：将孤立短字符与前面的短拼音合并（如 'xi' + 'o' → 'xio'）
    corrected = []
    for p in result:
        if corrected and len(p) == 1 and len(corrected[-1]) <= 2:
            # 单字符，前面是短拼音（如 xi, shi 等），合并
            corrected[-1] = corrected[-1] + p
        else:
            corrected.append(p)
    result = corrected

    # 如果切分结果数量不等于汉字数量，尝试合并相邻的短拼音
    if len(result) != hanzi_count:
        result = _merge_short_pinyins(result, hanzi_count)

    return result


def _merge_short_pinyins(pinyins: List[str], target_count: int) -> List[str]:
    """
    尝试合并相邻的短拼音或无效拼音，使总数接近目标数量。
    策略：优先合并后形成有效拼音的相邻片段，
          其次合并包含无效拼音的相邻片段，
          避免合并两个独立的有效拼音（除非长度极短，可能是切分错误）。
    """
    if len(pinyins) <= target_count:
        return pinyins

    # 需要合并的次数
    merge_times = len(pinyins) - target_count

    result = list(pinyins)

    for _ in range(merge_times):
        if len(result) <= target_count:
            break

        best_merge_idx = -1
        best_score = -1000

        for i in range(len(result) - 1):
            # 计算合并得分
            score = 0
            p1, p2 = result[i], result[i + 1]
            p1_no_tone = remove_tone(p1)
            p2_no_tone = remove_tone(p2)
            merged_no_tone = p1_no_tone + p2_no_tone

            # 如果合并后形成有效拼音，最高优先级
            if merged_no_tone in VALID_PINYINS:
                score += 300
            # 如果两个都是无效拼音，高优先级合并
            elif p1_no_tone not in VALID_PINYINS and p2_no_tone not in VALID_PINYINS:
                score += 200
            # 如果一个是无效拼音，另一个是短拼音
            elif p1_no_tone not in VALID_PINYINS or p2_no_tone not in VALID_PINYINS:
                score += 100
                if len(p1) <= 2:
                    score += 20 - len(p1) * 5
                if len(p2) <= 2:
                    score += 20 - len(p2) * 5
            # 两个都是有效拼音：只合并极短的（可能是切分错误）
            else:
                if len(p1) == 1:
                    score += 50
                elif len(p1) == 2:
                    score += 10
                if len(p2) == 1:
                    score += 50
                elif len(p2) == 2:
                    score += 10

            if score > best_score:
                best_score = score
                best_merge_idx = i

        if best_merge_idx >= 0:
            # 合并相邻的两个拼音
            merged = result[best_merge_idx] + result[best_merge_idx + 1]
            result[best_merge_idx] = merged
            del result[best_merge_idx + 1]
        else:
            # 如果没有找到好的合并位置，从后往前合并
            result[-2] = result[-2] + result[-1]
            del result[-1]

    return result


def is_chinese_char(ch: str) -> bool:
    """判断字符是否为汉字"""
    return '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'


def clean_pinyin_line(line: str) -> str:
    """过滤掉拼音行中的非拼音字符（如标点、空格等）"""
    return ''.join(ch for ch in line if ch in _PINYIN_CHARS)


def process_pair(pinyin_line: str, hanzi_line: str) -> str:
    """
    将一行拼音和一行汉字逐字配对。
    标点符号保留原位，汉字后紧跟拼音。
    注意：会忽略拼音行和汉字行中的空格。
    策略：优先使用原始空格分隔的拼音按顺序对应，不再使用连续字符串切分，
          避免错误拼音（如xio）被智能切分破坏对应关系。
    """
    pinyin_line = pinyin_line.strip()
    hanzi_line = hanzi_line.strip()

    # 统计汉字数量（忽略空格）
    hanzi_count = sum(1 for ch in hanzi_line if is_chinese_char(ch))

    # 将拼音行中的常见标点替换为空格，避免标点粘连导致拼音合并（如 "shǔ，tān"）
    _punctuations = "，,。.;：;!！?？、（）()【】[]《》<>·\"'\n\u201c\u201d\u2026"
    cleaned_pinyin_line = pinyin_line
    for ch in _punctuations:
        cleaned_pinyin_line = cleaned_pinyin_line.replace(ch, ' ')

    # 按空格分隔拼音
    pinyin_parts = [p.strip() for p in cleaned_pinyin_line.split() if p.strip()]

    # 检查：空格分隔的片段都较长（>5字符），说明空格不是拼音分隔符
    # 此时应忽略空格，按连续字符串处理
    if pinyin_parts and all(len(p) > 5 for p in pinyin_parts) and len(pinyin_parts) < hanzi_count:
        pinyin_parts = [''.join(pinyin_parts)]

    if len(pinyin_parts) >= hanzi_count:
        # 拼音数量足够或更多，截取前 hanzi_count 个
        pinyins = pinyin_parts[:hanzi_count]
    elif len(pinyin_parts) == 1:
        # 只有一个拼音片段，可能是连续字符串，尝试智能切分
        pinyin_clean = clean_pinyin_line(pinyin_line)
        pinyins = split_pinyin(pinyin_clean, hanzi_count)
        # 如果切分出的数量 > 汉字数量，说明有过度切分，回退到原始拼音
        if len(pinyins) > hanzi_count:
            pinyins = [pinyin_parts[0]] + [""] * (hanzi_count - 1)
        # 如果切分出的数量 <= 汉字数量，说明拼音不足或刚好，直接使用
        # 不足的用空字符串补齐
    else:
        # 拼音数量不足，用空字符串补齐（保持原始空格分隔的对应关系）
        pinyins = pinyin_parts + [""] * (hanzi_count - len(pinyin_parts))

    result_parts = []
    pinyin_idx = 0

    for ch in hanzi_line:
        # 跳过空格
        if ch == ' ' or ch == '\t':
            continue
        if is_chinese_char(ch):
            if pinyin_idx < len(pinyins) and pinyins[pinyin_idx]:
                result_parts.append(f"{ch}{pinyins[pinyin_idx]}")
                pinyin_idx += 1
            else:
                # 没有对应拼音，只保留汉字
                result_parts.append(ch)
                pinyin_idx += 1
        else:
            # 保留标点符号等非汉字字符
            result_parts.append(ch)

    return ''.join(result_parts)


def assemble_pinyin_text(text: str) -> str:
    """
    解析并组装拼音文本。
    支持两种格式：
    1. "拼音行+汉字行"交替格式（多行）
    2. 单行内"拼音在前+汉字在后"混合格式
    如果不是这些格式，则原样返回。
    """
    lines = text.strip().splitlines()
    if not lines:
        return text

    # 检测是否为"拼音行+汉字行"交替格式
    def is_mostly_pinyin(line: str) -> bool:
        """判断一行是否主要是拼音（字母+声调符号）"""
        if not line.strip():
            return False
        # 移除空格和标点后计算拼音字符比例
        _puncts = "，,。.;：;!！?？、（）()【】[]《》<>·\"'\"'\n\t \u2026\u201c\u201d"
        cleaned = line
        for ch in _puncts:
            cleaned = cleaned.replace(ch, '')
        if not cleaned:
            return False
        pinyin_chars = sum(1 for ch in line if ch in _PINYIN_CHARS)
        return pinyin_chars / max(len(cleaned), 1) > 0.7

    def is_mostly_hanzi(line: str) -> bool:
        """判断一行是否主要是汉字"""
        if not line.strip():
            return False
        hanzi_chars = sum(1 for ch in line if is_chinese_char(ch))
        return hanzi_chars / max(len(line.strip()), 1) > 0.3

    # 检查前2行是否符合"拼音行+汉字行"模式
    if len(lines) >= 2:
        first = lines[0].strip()
        second = lines[1].strip()
        # 如果第0行主要是拼音，第1行主要是汉字，则认为是目标格式
        if is_mostly_pinyin(first) and is_mostly_hanzi(second):
            # 解析并组装多行交替格式
            output_lines = []
            i = 0
            while i < len(lines):
                if i + 1 < len(lines):
                    pinyin_line = lines[i]
                    hanzi_line = lines[i + 1]
                    if is_mostly_hanzi(hanzi_line):
                        merged = process_pair(pinyin_line, hanzi_line)
                        output_lines.append(merged)
                        i += 2
                    else:
                        output_lines.append(lines[i])
                        i += 1
                else:
                    output_lines.append(lines[i])
                    i += 1
            return '\n'.join(output_lines)

    # 如果不是多行交替格式，尝试处理单行混合格式
    # 单行格式：拼音在前，汉字在后，如 "hěn jiǔ yǐ qián很 久 以 前，"
    if len(lines) == 1:
        line = lines[0].strip()
        # 查找拼音部分和汉字部分的分界点
        # 策略：从左到右扫描，找到第一个汉字出现的位置
        first_hanzi_pos = -1
        for i, ch in enumerate(line):
            if is_chinese_char(ch):
                first_hanzi_pos = i
                break

        if first_hanzi_pos > 0:
            # 有拼音和汉字两部分
            pinyin_part = line[:first_hanzi_pos].strip()
            hanzi_part = line[first_hanzi_pos:].strip()

            # 验证拼音部分是否主要是拼音字符
            pinyin_chars = sum(1 for ch in pinyin_part if ch in _PINYIN_CHARS)
            if pinyin_chars / max(len(pinyin_part), 1) > 0.5:
                # 是有效的拼音+汉字格式，进行组装
                return process_pair(pinyin_part, hanzi_part)

    # 不符合任何已知格式，原样返回
    return text


class MiduProofreadError(RuntimeError):
    pass


def load_business_credentials() -> tuple[str, str, str]:
    """成对读取宿主注入或独立 Box-Agent 保存的凭证。"""
    api_key, user_id, source = load_credentials()
    if api_key and user_id:
        return api_key, user_id, source
    raise MiduProofreadError(AUTH_HINT)


@dataclass(frozen=True)
class ProofreadUrls:
    proof_json_url: str
    erratum_excel_url: str
    erratum_md_url: str
    transaction_id: str
    char_count: Optional[int]


@dataclass(frozen=True)
class ProofreadOutputs:
    urls: ProofreadUrls
    erratum_md: str


def call_proofread_api(
    proof_text: str,
    api_key: str,
    user_id: str,
    credential_source: str,
    file_name: Optional[str] = None,
) -> ProofreadUrls:
    headers = {
        "Content-Type": "application/json",
        "X-Skill-Code": SKILL_CODE,
        "Authorization": f"Bearer {api_key}",
        "X-User-Id": user_id,
    }
    params: Dict[str, Any] = {
        "proofText": proof_text,
    }
    if file_name and file_name.strip():
        params["fileName"] = os.path.basename(file_name.strip())

    try:
        r = requests.post(API_URL, json=params, headers=headers, timeout=60)
    except requests.RequestException as exc:
        raise MiduProofreadError(f"网络请求失败：{exc}") from exc
    if r.status_code in (401, 403):
        if looks_like_no_entitlement(r.text):
            raise MiduProofreadError(RECHARGE_HINT)
        raise MiduProofreadError(
            f"鉴权失败或凭证失效（HTTP {r.status_code}）。\n"
            f"{build_auth_hint(credential_source=credential_source)}"
        )
    if not r.ok:
        if looks_like_no_entitlement(r.text):
            raise MiduProofreadError(RECHARGE_HINT)
        raise MiduProofreadError(f"接口 HTTP 错误: {r.status_code} {r.text}".strip())
    try:
        resp = r.json()
    except ValueError as exc:
        raise MiduProofreadError(f"响应不是合法 JSON：{r.text}") from exc

    code = resp.get("code")
    msg = resp.get("msg", "")
    txid = resp.get("transactionId", "")
    char_count = resp.get("charCount")
    if str(code).strip() != "0000":
        if looks_like_no_entitlement(msg) or looks_like_no_entitlement(code):
            raise MiduProofreadError(RECHARGE_HINT)
        if looks_like_auth_failure(code, msg):
            raise MiduProofreadError(
                "鉴权失败或凭证失效。"
                + (f"transactionId={txid}\n" if txid else "")
                + build_auth_hint(credential_source=credential_source)
            )
        raise MiduProofreadError(f"接口调用失败: code={code}, msg={msg}" + (f", transactionId={txid}" if txid else ""))

    data = resp.get("data") or {}
    proof_json_url = data.get("proofResultJsonUrl")
    excel_url = data.get("erratumExcelUrl")
    md_url = data.get("erratumMdUrl")
    if not (proof_json_url and excel_url and md_url):
        raise MiduProofreadError(f"响应缺少必要 URL 字段: {json.dumps(resp, ensure_ascii=False)}")

    return ProofreadUrls(
        proof_json_url=str(proof_json_url),
        erratum_excel_url=str(excel_url),
        erratum_md_url=str(md_url),
        transaction_id=str(txid),
        char_count=int(char_count) if isinstance(char_count, (int, float)) else None,
    )


def proofread_text(
    proof_text: str,
    file_name: Optional[str] = None,
    assemble: bool = True,
) -> ProofreadOutputs:
    if not proof_text.strip():
        raise MiduProofreadError("proofText 不能为空。")

    # 步骤 1：如果文本是"拼音行+汉字行"交替格式，先组装为"汉字+拼音"格式
    if assemble:
        assembled_text = assemble_pinyin_text(proof_text)
    else:
        assembled_text = proof_text

    key, uid, credential_source = load_business_credentials()

    # 步骤 2：调用校对接口
    urls = call_proofread_api(
        proof_text=assembled_text,
        api_key=key,
        user_id=uid,
        credential_source=credential_source,
        file_name=file_name,
    )

    return ProofreadOutputs(
        urls=urls,
        erratum_md="",
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Midu 拼音校对：解析拼音行+汉字行交替格式，组装后调用校对接口并展示 Markdown（不落盘下载）")
    p.add_argument("--text", default=None, help="待校对纯文本。支持'拼音行+汉字行'交替格式，会自动组装为'汉字+拼音'格式后校对。")
    p.add_argument(
        "--file-name",
        dest="file_name",
        default=None,
        help="来源文件名（仅 basename，可选）；用于日志追踪",
    )
    return p


def main(argv: Optional[Tuple[str, ...]] = None) -> int:
    args = _build_arg_parser().parse_args(list(argv) if argv is not None else None)

    # 支持从 stdin 读取文本（当 --text 未指定时）
    text_input = args.text
    if not text_input:
        if not sys.stdin.isatty():
            # 有管道输入，从 stdin 读取
            text_input = sys.stdin.read()
        else:
            print("❌ 错误: 必须指定 --text 参数或通过 stdin 管道传入待校对文本。", file=sys.stderr)
            return 2

    if not text_input.strip():
        print(" 错误: 待校对文本不能为空。", file=sys.stderr)
        return 2

    try:
        outputs = proofread_text(
            text_input,
            file_name=args.file_name,
        )
        # 从 URL 中提取文件名作为链接文本
        json_filename = outputs.urls.proof_json_url.split('/')[-1]
        excel_filename = outputs.urls.erratum_excel_url.split('/')[-1]
        md_filename = outputs.urls.erratum_md_url.split('/')[-1]

        # 构建输出内容：直接以 Markdown 形式展示勘误内容
        output_parts = []
        if outputs.erratum_md and outputs.erratum_md.strip():
            output_parts.append(outputs.erratum_md.rstrip())
        else:
            output_parts.append(
                "> 校对已完成。为避免自动访问服务端返回的任意地址，"
                "脚本未下载勘误 Markdown，请使用下方结果链接。"
            )
        output_parts.append("")
        output_parts.append("---")
        output_parts.append("")
        output_parts.append(f"- 校对结果 JSON: [{json_filename}]({outputs.urls.proof_json_url})")
        output_parts.append(f"- 勘误表 Excel: [{excel_filename}]({outputs.urls.erratum_excel_url})")
        output_parts.append(f"- 勘误 Markdown: [{md_filename}]({outputs.urls.erratum_md_url})")
        output_parts.append(f"- **transactionId**: `{outputs.urls.transaction_id}`")

        print("\n".join(output_parts))
        return 0
    except MiduProofreadError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
