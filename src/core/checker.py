"""
生日检查模块
"""
from datetime import datetime, timedelta, date
from typing import List, Optional, Tuple, Dict, Union
from lunar_python import Lunar, Solar
from src.core.config import Recipient
import logging

logger = logging.getLogger(__name__)


class BirthdayChecker:
    def check_birthdays(self, recipients: List[Recipient]) -> List[Tuple[Recipient, bool, Dict]]:
        """
        检查所有人的生日

        Args:
            recipients: 收件人列表

        Returns:
            List[Tuple[Recipient, bool, Dict]]: 返回收件人、是否生日和额外信息的元组列表
        """
        today = datetime.now()
        results = []

        for recipient in recipients:
            is_birthday, extra_info = self._check_birthday(recipient, today)
            results.append((recipient, is_birthday, extra_info))

        return results

    def _convert_to_date_parts(self, date_obj: Union[str, datetime, date]) -> Tuple[int, int, int]:
        """
        转换日期为年月日元组

        Args:
            date_obj: 日期对象，可以是字符串、datetime或date类型

        Returns:
            Tuple[int, int, int]: 年月日的元组
        """
        if isinstance(date_obj, str):
            dt = datetime.strptime(date_obj, "%Y-%m-%d")
        elif isinstance(date_obj, datetime):
            dt = date_obj
        elif isinstance(date_obj, date):
            dt = datetime.combine(date_obj, datetime.min.time())
        else:
            raise ValueError(f"Unsupported date type: {type(date_obj)}")
        return (dt.year, dt.month, dt.day)

    @staticmethod
    def _birth_month_day(value: str) -> Optional[Tuple[int, int, Optional[int]]]:
        """解析生日字段，返回 ``(月, 日, 出生年或 None)``。

        支持两种写法：

        - ``1990-06-06`` → ``(6, 6, 1990)``，能算年龄
        - ``0606`` / ``06-06`` → ``(6, 6, None)``，只有月日

        **只用月日参与匹配**，年份仅用于算年龄；无年时返回 ``None``，
        调用方相应地不显示"满 X 岁"。

        不接受非法月日（``0230`` 这类）—— 与 web 层同一原则：
        宁可报错，不给假数据。
        """
        text = (value or "").strip()
        if not text:
            return None

        # 完整日期
        try:
            dt = datetime.strptime(text, "%Y-%m-%d")
            return dt.month, dt.day, dt.year
        except ValueError:
            pass

        # 只有月日：0606 / 06-06 / 6-6
        digits = text.replace(" ", "")
        if "-" in digits:
            parts = [x for x in digits.split("-") if x]
            if len(parts) != 2 or not all(x.isdigit() for x in parts):
                return None
            month, day = int(parts[0]), int(parts[1])
        elif len(digits) == 4 and digits.isdigit():
            month, day = int(digits[:2]), int(digits[2:])
        else:
            return None

        # 用闰年校验真实性：容纳 2 月 29 日，拒绝 0230 / 1301
        try:
            date(2000, month, day)
        except ValueError:
            return None
        return month, day, None

    @staticmethod
    def _next_solar_date(birth_str: str, today: datetime) -> Optional[date]:
        """阳历生日（月/日）的**下一次**出现日期，不早于今天。

        与匹配逻辑同口径：只看月日。今天是今天，下一次可能是今年（没过）或明年。
        接受"只有月日"的写法（如 0606）—— 年份不参与计算。
        """
        parsed = BirthdayChecker._birth_month_day(birth_str)
        if parsed is None:
            return None
        month, day = parsed[0], parsed[1]
        try:
            target = datetime(today.year, month, day)
        except ValueError:
            # 2 月 29 日在非闰年不存在：往后再找一个闰年
            for offset in range(1, 5):
                try:
                    return datetime(today.year + offset, month, day).date()
                except ValueError:
                    continue
            return None
        if target < datetime(today.year, today.month, today.day):
            try:
                target = datetime(today.year + 1, month, day)
            except ValueError:
                for offset in range(2, 6):
                    try:
                        return datetime(today.year + offset, month, day).date()
                    except ValueError:
                        continue
                return None
        return target.date()

    @staticmethod
    def _next_lunar_solar_date(lunar_str: str, today: datetime) -> Optional[date]:
        """农历生日（月/日）下一次对应的**阳历日期**，不早于今天。

        用 Lunar 反查而不是逐日扫描：Solar.getLunar 每次重算整年农历，
        扫 400 天约 700ms；Lunar.fromYmd(...).getSolar() 直接反查，
        最多试 3 个农历年，快 20 倍以上。与 src/web/domain 的同名函数口径一致。
        """
        parsed = BirthdayChecker._birth_month_day(lunar_str)
        if parsed is None:
            return None
        month, day = parsed[0], parsed[1]

        for lunar_year in range(today.year, today.year + 3):
            try:
                lunar = Lunar.fromYmd(lunar_year, month, day)
                solar = lunar.getSolar()
            except Exception:
                continue
            if solar is None:
                continue
            y, m, d = solar.getYear(), solar.getMonth(), solar.getDay()
            candidate = datetime(y, m, d)
            if candidate >= datetime(today.year, today.month, today.day):
                return candidate.date()
        return None

    def _check_birthday(self, recipient: Recipient, today: datetime) -> Tuple[bool, Dict]:
        """
        检查是否是生日（包括提前提醒）

        Args:
            recipient: 收件人信息
            today: 当前日期

        Returns:
            Tuple[bool, Dict]: 是否是生日和额外信息
        """
        try:
            # 提醒窗口 = 生日前第 reminder_days 天 … 生日前一天。
            # **不含生日当天**（i 从 1 开始）：生日到了就不必再提醒"该给谁过生日"了，
            # 那时祝福应该已经送到。以 22 号生日、reminder_days=3 为例，
            # 19/20/21 号早上各发一次，22 号早上不发。
            check_dates = []
            for i in range(1, recipient.reminder_days + 1):
                check_date = today + timedelta(days=i)
                check_dates.append(check_date)

            extra_info = {
                'solar_match': False,
                'lunar_match': False,
                'days_until': 0,
                'gz_year': '',          # 干支纪年
                'gz_month': '',         # 干支纪月
                'gz_day': '',           # 干支纪日
                'gz_hour': '',          # 干支纪时
                'lunar_month': '',      # 农历月份
                'lunar_day': '',        # 农历日期
                'lunar_festival': '',   # 农历节日
                'solar_festival': '',   # 阳历节日
                'solar_term': '',       # 节气
                'age': 0,
                'week_name': '',        # 星期
                'constellation': '',    # 星座
                # 邮件模板用它们拼「今天 YYYY-MM-DD 星期X」那一行。
                # 它们一直在下面算出来了，只是忘了放进 extra_info ——
                # 于是 run 命令渲染邮件时抛 UndefinedError: 'year' is undefined，
                # 而推送渠道不看这几个字段，所以只在发邮件时才炸。
                'year': 0,
                'month': 0,
                'day': 0,
            }

            # 获取今天的详细信息
            year, month, day = self._convert_to_date_parts(today)
            today_solar = Solar.fromYmd(year, month, day)
            today_lunar = today_solar.getLunar()

            # 填充当天信息
            extra_info.update({
                'year': year,
                'month': month,
                'day': day,
                'gz_year': today_lunar.getYearInGanZhi(),
                'gz_month': today_lunar.getMonthInGanZhi(),
                'gz_day': today_lunar.getDayInGanZhi(),
                'gz_hour': today_lunar.getTimeInGanZhi(),
                'lunar_month': f"{today_lunar.getMonthInChinese()}月",
                'lunar_day': today_lunar.getDayInChinese(),
                'week_name': today_solar.getWeekInChinese(),
                'constellation': today_solar.getXingZuo(),
            })

            # 获取节日信息
            lunar_festivals = today_lunar.getFestivals()
            if lunar_festivals:
                extra_info['lunar_festival'] = '、'.join(lunar_festivals)

            solar_festivals = today_solar.getFestivals()
            if solar_festivals:
                extra_info['solar_festival'] = '、'.join(solar_festivals)

            # 获取节气
            term = today_lunar.getJieQi()
            if term:
                extra_info['solar_term'] = term

            # 检查阳历生日。允许"只有月日"（如 0606）—— 匹配只看月日，
            # 只是算不出年龄。
            if recipient.solar_birthday:
                parsed = self._birth_month_day(recipient.solar_birthday)
                if parsed is None:
                    logger.error(
                        f"Invalid solar birthday format for {recipient.name}: "
                        f"{recipient.solar_birthday}"
                    )
                else:
                    birth_month, birth_day, birth_year = parsed
                    for i, check_date in enumerate(check_dates, start=1):
                        year, month, day = self._convert_to_date_parts(check_date)
                        if month == birth_month and day == birth_day:
                            extra_info['solar_match'] = True
                            extra_info['days_until'] = i
                            if birth_year is not None:
                                extra_info['age'] = year - birth_year
                            break

            # 检查农历生日。同样允许只有月日（如 1224 = 腊月廿四）。
            if recipient.lunar_birthday:
                parsed = self._birth_month_day(recipient.lunar_birthday)
                if parsed is None:
                    logger.error(
                        f"Invalid lunar birthday format for {recipient.name}: "
                        f"{recipient.lunar_birthday}"
                    )
                else:
                    birth_month, birth_day, birth_year = parsed
                    for i, check_date in enumerate(check_dates, start=1):
                        year, month, day = self._convert_to_date_parts(check_date)
                        check_solar = Solar.fromYmd(year, month, day)
                        check_lunar = check_solar.getLunar()
                        if (check_lunar.getMonth() == birth_month
                                and check_lunar.getDay() == birth_day):
                            extra_info['lunar_match'] = True
                            extra_info['days_until'] = i
                            if birth_year is not None:
                                extra_info['age'] = year - birth_year
                            break

            # 两个生日各自的下一次日期（独立于窗口命中与否）：
            # 通知文案要两行都写 —— 阳历生日、农历生日各一行，各自带日期。
            # 给寿星的祝福短信两条都告诉他，他才知道哪个日子对得上自己。
            if recipient.solar_birthday:
                nxt = self._next_solar_date(recipient.solar_birthday, today)
                if nxt is not None:
                    extra_info['solar_next_date'] = nxt.isoformat()
            if recipient.lunar_birthday:
                nxt = self._next_lunar_solar_date(recipient.lunar_birthday, today)
                if nxt is not None:
                    extra_info['lunar_next_solar_date'] = nxt.isoformat()

            # 命中与否只看日历匹配结果。
            # 曾经这里还补生肖（zodiac），但正文已不再渲染它 ——
            # 通知文案是给寿星的祝福短信，不是黄历（见 sender_serverchan 的说明）。
            # 匹配逻辑与 extra_info 里的农历信息（lunar_month / lunar_day）无关，
            # 那些仍然保留，供需要时使用。
            return (extra_info['solar_match'] or extra_info['lunar_match']), extra_info
        except Exception as e:
            logger.error(f"Error checking birthday for {recipient.name}: {e}")
            return False, {}
