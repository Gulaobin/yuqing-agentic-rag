"""生成器的固定参数：时间基准、规模、平台、派生规则和事件目录。

事件、地名、机构名均为虚构，如有雷同纯属巧合。
"""

from __future__ import annotations

from datetime import datetime

GENERATOR_VERSION = "0.1.0"
DEFAULT_SEED = 20261103

# 所有时间都是 Asia/Shanghai 本地时间，存成 "YYYY-MM-DD HH:MM:SS"，不带时区。
TIME_FMT = "%Y-%m-%d %H:%M:%S"
BASE_TIME = datetime(2026, 9, 1, 0, 0, 0)
WINDOW_HOURS = 72  # 每个事件只保留首帖之后 72 小时内的帖子

N_BASE_ACCOUNTS = 1500
ACCOUNT_CREATED_FROM = datetime(2015, 1, 1)
# 普通账号都在此之前注册，事件最早 9 月 1 日开始，所以普通账号发帖时注册均超过 30 天；
# 「注册不到 7 天」的账号只来自埋入的规律。
ACCOUNT_CREATED_UNTIL = datetime(2026, 8, 1)

PLATFORMS = ("weibo", "douyin", "xiaohongshu", "news")
ACCOUNT_PLATFORM_WEIGHTS = {"weibo": 0.50, "douyin": 0.22, "xiaohongshu": 0.18, "news": 0.10}
SENTIMENTS = ("negative", "neutral", "positive")

# 派生规则：events.level 与 events.status 由帖子数据按下列规则算出，不随机指定。
# 热度 = metrics_hourly.post_count（该小时全部帖子，含转发）。
LEVEL_THRESHOLDS = ((600, 1), (350, 2), (150, 3))  # 72 小时总帖数 >= 阈值 → 等级；都不满足为 4 级
RISING_PEAK_HOUR = 48  # 峰值小时 >= 48 → rising
SUBSIDED_TAIL_HOURS = (60, 72)  # 最后 12 小时帖数占比 <= 2% → subsided；其余为 declining
SUBSIDED_TAIL_SHARE = 0.02
NEW_ACCOUNT_DAYS = 7  # 「新注册账号」：发帖时注册不足 7 天


def level_for(total_posts: int) -> int:
    for threshold, level in LEVEL_THRESHOLDS:
        if total_posts >= threshold:
            return level
    return 4


def status_for(hourly_posts: list[int]) -> str:
    total = sum(hourly_posts)
    peak = max(range(len(hourly_posts)), key=lambda h: (hourly_posts[h], -h))  # 并列取最早
    if peak >= RISING_PEAK_HOUR:
        return "rising"
    start, end = SUBSIDED_TAIL_HOURS
    if total and sum(hourly_posts[start:end]) / total <= SUBSIDED_TAIL_SHARE:
        return "subsided"
    return "declining"


# (主题, 标题, 关键词)。顺序即 event_id 顺序，首帖时间随序号递增。
EVENT_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("食品安全", "云澜市某连锁茶饮门店被曝使用过期原料", "过期原料"),
    ("消费维权", "杉溪县健身房闭店跑路，会员预付费难退", "闭店退费"),
    ("公共交通", "鹭港地铁4号线早高峰故障停运40分钟", "地铁停运"),
    ("教育", "槐南区某小学午餐分量引家长质疑", "午餐分量"),
    ("医疗", "云澜市第一医院挂号系统宕机，患者排长队", "挂号宕机"),
    ("环境", "白鹭河下游出现大面积死鱼", "死鱼"),
    ("企业经营", "禾沐科技被曝大规模裁员", "裁员"),
    ("网络谣言", "网传鹭港某商场发生踩踏，官方辟谣", "踩踏传闻"),
    ("文旅", "雾栖古镇景区门票涨价引游客不满", "门票涨价"),
    ("城市管理", "槐南区占道经营整治引发摊贩争议", "占道整治"),
    ("食品安全", "杉溪县校园周边小吃店卫生问题被曝光", "小吃卫生"),
    ("消费维权", "某二手车平台被指隐瞒事故车况", "事故车"),
    ("公共交通", "云澜机场航班大面积延误，旅客滞留", "航班延误"),
    ("教育", "鹭港某培训机构课时费纠纷", "课时费"),
    ("医疗", "槐南区社区卫生服务中心疫苗预约难", "疫苗预约"),
    ("环境", "杉溪县化工园区异味扰民", "异味扰民"),
    ("企业经营", "某外卖平台骑手抗议配送规则调整", "配送规则"),
    ("网络谣言", "网传云澜市自来水被污染", "自来水传闻"),
    ("文旅", "望舒山景区缆车故障，游客悬空半小时", "缆车故障"),
    ("城市管理", "鹭港老旧小区加装电梯协商陷入僵局", "加装电梯"),
    ("消费维权", "多家餐厅被指用预制菜冒充现炒", "预制菜"),
    ("公共交通", "杉溪县公交线路调整，居民出行不便", "公交调整"),
    ("教育", "槐南中学课后服务收费争议", "课后服务"),
    ("医疗", "某民营医院被投诉过度检查", "过度检查"),
    ("环境", "云澜湿地公园施工被指破坏生态", "湿地施工"),
    ("企业经营", "禾沐科技App被传泄露用户信息", "信息泄露"),
    ("网络谣言", "网传鹭港将全面限购新能源车", "限购传闻"),
    ("文旅", "雾栖古镇民宿虚假宣传被曝光", "虚假宣传"),
    ("城市管理", "槐南区共享单车乱停放整治", "单车整治"),
    ("食品安全", "超市冷链肉制品被曝篡改生产日期", "篡改日期"),
)


def event_id_for(index: int) -> str:
    return f"EVT-{index + 1:04d}"
