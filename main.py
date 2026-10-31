# -*- coding:utf-8 -*-
import os
import requests
import hashlib
import time
import copy
import logging
import random

import smtplib
from email.mime.text import MIMEText


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# API_URL
# 2026 年起贴吧服务端已经不再接受老客户端（9.7.8.0），旧接口会「静默」返回空列表
CLIENT_VERSION = "22.12.1.0"

GUIDE_URL = "http://tieba.baidu.com/c/f/forum/forumGuide"   # 本账号关注的贴吧（网页版，无需 uid）
MSIGN_URL = "http://tieba.baidu.com/c/c/forum/msign"        # 一键签到（网页版）
LIKIE_URL = "http://tiebac.baidu.com/c/f/forum/like"        # 关注贴吧列表（App 版，需 friend_uid）
TBS_URL = "http://tieba.baidu.com/dc/common/tbs"            # 顺带返回 is_login
SIGN_URL = "http://tiebac.baidu.com/c/c/forum/sign"         # 单吧签到（手机版 / App 协议，需 tbs）

ENV = os.environ

# 签到方式：app=手机版逐吧签到（默认）；web=网页版一键签到 msign
SIGN_MODE = (ENV.get('SIGN_MODE') or 'app').strip().lower()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/39.0.2171.71 Safari/537.36',
}
HYBRID_HEADERS = dict(HEADERS, **{'Subapp-Type': 'hybrid'})
SIGN_DATA = {
    '_client_type': '2',
    '_client_version': CLIENT_VERSION,
    '_phone_imei': '000000000000000',
    'model': 'MI+5',
    "net_type": "1",
}

# VARIABLE NAME
COOKIE = "Cookie"
BDUSS = "BDUSS"
EQUAL = r'='
EMPTY_STR = r''
TBS = 'tbs'
PAGE_NO = 'page_no'
ONE = '1'
TIMESTAMP = "timestamp"
DATA = 'data'
FID = 'fid'
SIGN_KEY = 'tiebaclient!!!'
UTF8 = "utf-8"
SIGN = "sign"
KW = "kw"

s = requests.Session()


def check_login(bduss):
    """校验 BDUSS 是否有效，返回 (is_login, tbs)

    注意：/dc/common/tbs 在**未登录时也会返回一个匿名 tbs**（同时 is_login=0），
    所以「tbs 拿到非空」不能证明 BDUSS 有效，必须看 is_login。
    """
    logger.info("校验登录态开始")
    try:
        resp = s.get(url=TBS_URL, cookies={BDUSS: bduss}, headers=HEADERS, timeout=10).json()
    except Exception as e:
        logger.error(f"获取tbs出错: {e}")
        return 0, None
    is_login = int(resp.get("is_login") or 0)
    tbs = resp.get(TBS)
    if not is_login or not tbs:
        logger.error(f"BDUSS 无效或已过期，接口返回: {resp}")
        return 0, None
    logger.info("校验登录态结束")
    return is_login, tbs


def get_favorite(bduss):
    """获取本账号关注的贴吧（网页版 forumGuide）

    返回 [{fid, name, is_signed}]；is_signed 表示今天是否已经签过。
    """
    logger.info("获取关注的贴吧开始")
    try:
        res = s.post(url=GUIDE_URL,
                     data={'sort_type': '3', 'call_from': '3'},
                     cookies={BDUSS: bduss},
                     headers=HYBRID_HEADERS,
                     timeout=10).json()
    except Exception as e:
        logger.error(f"获取关注的贴吧出错: {e}")
        return []

    if int(res.get('error_code') or 0) != 0:
        logger.error(f"获取关注的贴吧失败 {res.get('error_code')}: {res.get('error_msg')}")
        return []
    if int(res.get('is_login') or 0) != 1:
        logger.error(f"获取关注的贴吧时判定未登录（BDUSS 无效），接口返回: {res}")
        return []

    forums = []
    for f in res.get('like_forum') or []:
        forums.append({
            'fid': int(f['forum_id']),
            'name': f['forum_name'],
            'is_signed': bool(f.get('is_sign')),
        })
    if res.get('like_forum_has_more'):
        logger.warning("关注的贴吧超出接口单次下发上限（约200个），部分吧可能签不到")
    logger.info(f"获取关注的贴吧结束，共 {len(forums)} 个，已签到 {sum(1 for f in forums if f['is_signed'])} 个")
    return forums


def encodeData(data):
    s = EMPTY_STR
    keys = data.keys()
    for i in sorted(keys):
        s += i + EQUAL + str(data[i])
    sign = hashlib.md5((s + SIGN_KEY).encode(UTF8)).hexdigest().upper()
    data.update({SIGN: str(sign)})
    return data


def sign_all(bduss):
    """一键签到本账号关注的全部贴吧（网页版 msign，无需 tbs）"""
    logger.info("开始一键签到")
    try:
        res = s.post(url=MSIGN_URL,
                     data={'_client_version': CLIENT_VERSION, 'subapp_type': 'hybrid'},
                     cookies={BDUSS: bduss},
                     headers=HYBRID_HEADERS,
                     timeout=10).json()
    except Exception as e:
        logger.error(f"一键签到出错: {e}")
        return False
    if str(res.get('error_code', '0')) != '0':
        logger.warning(f"一键签到返回错误码 {res.get('error_code')}: {res.get('error_msg')}")
        return False
    err = res.get('error') or {}
    if int(err.get('errno') or 0) != 0:
        logger.warning(f"一键签到返回错误码 {err.get('errno')}: {err.get('errmsg')}")
        return False
    logger.info(f"一键签到结束: {res.get('msign_text') or 'OK'}")
    return True


def client_sign(bduss, tbs, fid, kw):
    """手机版（App 协议）单个吧签到，需要 tbs

    域名 tiebac.baidu.com + _client_type=2 + 参数签名，就是手机客户端的签到请求。
    """
    data = copy.copy(SIGN_DATA)
    data.update({BDUSS: bduss, FID: str(fid), KW: kw, TBS: tbs, TIMESTAMP: str(int(time.time()))})
    data = encodeData(data)
    try:
        res = s.post(url=SIGN_URL, data=data, cookies={BDUSS: bduss}, headers=HEADERS, timeout=10).json()
    except Exception as e:
        logger.error(f"签到「{kw}」失败: {e}")
        return {"error": str(e)}
    if str(res.get("error_code", "0")) != "0":
        logger.warning(f"签到「{kw}」返回错误码 {res.get('error_code')}: {res.get('error_msg')}")
    else:
        info = res.get('user_info') or {}
        bonus = info.get('sign_bonus_point')
        extra = f"，+{bonus} 经验" if bonus else ""
        logger.info(f"签到「{kw}」成功{extra}")
    return res


def send_email(sign_list):
    if ('HOST' not in ENV or 'FROM' not in ENV or 'TO' not in ENV or 'AUTH' not in ENV):
        logger.error("未配置邮箱")
        return
    HOST = ENV['HOST']
    FROM = ENV['FROM']
    TO = ENV['TO'].split('#')
    AUTH = ENV['AUTH']
    length = len(sign_list)
    subject = f"{time.strftime('%Y-%m-%d', time.localtime())} 签到{length}个贴吧"
    body = """
    <style>
    .child {
      background-color: rgba(173, 216, 230, 0.19);
      padding: 10px;
    }

    .child * {
      margin: 5px;
    }
    </style>
    """
    for i in sign_list:
        body += f"""
        <div class="child">
            <div class="name"> 贴吧名称: { i['name'] }</div>
            <div class="slogan"> 贴吧简介: { i['slogan'] }</div>
        </div>
        <hr>
        """
    msg = MIMEText(body, 'html', 'utf-8')
    msg['subject'] = subject
    smtp = smtplib.SMTP()
    smtp.connect(HOST)
    smtp.login(FROM, AUTH)
    smtp.sendmail(FROM, TO, msg.as_string())
    smtp.quit()


def main():
    os.environ['TZ'] = 'Asia/Shanghai'
    if hasattr(time, 'tzset'):
        time.tzset()
    if ('BDUSS' not in ENV):
        logger.error("未配置BDUSS")
        return
    sign_list = []
    for n, bduss in enumerate(ENV['BDUSS'].split('#')):
        logger.info("开始签到第" + str(n) + "个用户")
        is_login, tbs = check_login(bduss)
        if not is_login:
            logger.error(f"第{n}个用户 BDUSS 无效或已过期，跳过（请重新获取 BDUSS）")
            continue
        favorites = get_favorite(bduss)
        if not favorites:
            logger.warning(f"第{n}个用户没有可签到的贴吧")
            continue

        todo = [f for f in favorites if not f['is_signed']]
        if not todo:
            logger.info(f"第{n}个用户今天已经全部签到过了")
        elif SIGN_MODE == 'web':
            sign_all(bduss)
        else:
            logger.info(f"第{n}个用户开始手机版签到，共 {len(todo)} 个吧")
            ok = 0
            for idx, f in enumerate(todo, 1):
                res = client_sign(bduss, tbs, f['fid'], f['name'])
                if str(res.get('error_code', '0')) == '0':
                    f['is_signed'] = True
                    ok += 1
                if idx != len(todo):
                    # 手机版接口对频率敏感，逐吧回访切勿并发
                    time.sleep(random.uniform(2.0, 4.0))
            logger.info(f"第{n}个用户手机版签到完成 {ok}/{len(todo)}")

        # 以服务端最终状态为准（已签到/签到失败）
        for f in get_favorite(bduss) or favorites:
            sign_list.append({
                'name': f['name'],
                'slogan': '签到成功' if f['is_signed'] else '签到失败',
            })
        logger.info("完成第" + str(n) + "个用户签到")

    failed = [i for i in sign_list if i['slogan'] != '签到成功']
    logger.info(f"所有用户签到结束：共 {len(sign_list)} 个吧，失败 {len(failed)} 个")
    for i in failed:
        logger.warning(f"未签到: {i['name']}")


if __name__ == '__main__':
    main()
