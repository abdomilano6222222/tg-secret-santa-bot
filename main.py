import datetime
import itertools
import json
import logging
import logging.config
import os
import random
import re
import threading
import time
from functools import wraps
from pathlib import Path
from random import choice
from typing import List, Callable, Optional, Union

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    BotCommandScopeAllChatAdministrators, ChatAction, ChatMemberLeft, ChatMemberUpdated, ChatMemberMember, \
    BotCommandScopeChatAdministrators, ChatMember
from telegram.error import BadRequest
from telegram.ext import Updater, CallbackContext, Filters, MessageHandler, CallbackQueryHandler, MessageFilter, \
    CommandHandler, ExtBot, Defaults, ChatMemberHandler
from telegram.utils.request import Request

import keyboards
import utilities
from emojis import Emoji
from santa import SecretSanta
from santa import NAME_MAX_LENGTH
from mwt import MWT
from config import config

ACTIVE_SECRET_SANTA_KEY = "active_secret_santa"
MUTED_KEY = "muted"
REMOVED_KEY = "removed"
BLOCKED_KEY = "blocked"
RECENTLY_LEFT_KEY = "recently_left"
RECENTLY_STARTED_SANTAS_KEY = "recently_closed_santas"

EMPTY_SECRET_SANTA_STR = f'{Emoji.SANTA}{Emoji.TREE} لم ينضم أحد إلى هذا السر سانتا بعد! استخدم زر "<b>انضم</b>" أدناه للانضمام'

class Time:
    WEEK_4 = 60 * 60 * 24 * 7 * 4
    WEEK_2 = 60 * 60 * 24 * 7 * 2
    WEEK_1 = 60 * 60 * 24 * 7
    DAY_3 = 60 * 60 * 24 * 3
    DAY_1 = 60 * 60 * 24
    HOUR_48 = 60 * 60 * 48
    HOUR_12 = 60 * 60 * 12
    HOUR_6 = 60 * 60 * 6
    HOUR_1 = 60 * 60
    MINUTE_30 = 60 * 30
    MINUTE_1 = 60


class Error:
    SEND_MESSAGE_DISABLED = "ليس لديك حقوق لإرسال رسالة"
    REMOVED_FROM_GROUP = "تم طرد البوت من"  # قد يتبعها "دردشة المجموعة" أو "دردشة السوبرغروب"
    CANT_EDIT = "chat_write_forbidden"  # نتلقى هذا عندما نحاول تعديل رسالة/الرد على استعلام رد ولكننا مكتومين
    MESSAGE_TO_EDIT_NOT_FOUND = "الرسالة المراد تعديلها غير موجودة"
    MESSAGE_NOT_MODIFIED = "الرسالة لم تتغير"
    USER_BLOCKED_BOT = "تم حظر البوت من قبل المستخدم"


class Commands:
    PRIVATE = [BotCommand("help", "رسالة الترحيب")]
    GROUP_ADMINISTRATORS = [
        BotCommand("newsanta", "إنشاء سر سانتا جديد في هذه الدردشة"),
        BotCommand("cancel", "إلغاء أي سر سانتا جارٍ"),
        BotCommand("hidecommands", "إخفاء هذه الأوامر"),
    ]


updater = Updater(
    bot=ExtBot(
        token=config.telegram.token,
        defaults=Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True),
        request=Request(con_pool_size=config.telegram.get('workers', 1) + 4)
    ),
    workers=0,
    persistence=utilities.persistence_object()
)

BOT_LINK = f"https://t.me/{updater.bot.username}"


class NewGroup(MessageFilter):
    def filter(self, message):
        if message.new_chat_members:
            member: User
            for member in message.new_chat_members:
                if member.id == updater.bot.id:
                    return True


def load_logging_config(file_name='logging.json'):
    with open(file_name, 'r') as f:
        logging_config = json.load(f)

    logging.config.dictConfig(logging_config)


load_logging_config("logging.json")

logger = logging.getLogger(__name__)


@MWT(timeout=60 * 60)
def get_admin_ids(bot: Bot, chat_id: int):
    return [admin.user.id for admin in bot.get_chat_administrators(chat_id)]


def administrators(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("فشل التحقق من المدير للرد <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def superadmin(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in config.telegram.admins:
            logger.debug("فشل التحقق من السوبرادمن للرد <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def users(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("فشل التحقق من المستخدم")
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def bot_restricted_check():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            if MUTED_KEY in context.chat_data:
                logger.info("استقبلت تحديث من الدردشة %d، لكننا مكتومون", update.effective_chat.id)
                return

            if REMOVED_KEY in context.chat_data:
                logger.info("استقبلت تحديث من الدردشة %d، لكننا تم إزالتنا", update.effective_chat.id)
                return

            try:
                return func(update, context, *args, **kwargs)
            except (TelegramError, BadRequest) as e:
                error_str = str(e).lower()
                if Error.REMOVED_FROM_GROUP in error_str:
                    logger.info("تمت الإزالة من الدردشة %d: تنظيف البيانات", update.effective_chat.id)
                    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
                elif Error.SEND_MESSAGE_DISABLED in error_str or Error.CANT_EDIT in error_str:
                    logger.info("لا يمكن إرسال الرسائل في الدردشة %d: يتم وضع علامة عليها كمكتومة", update.effective_chat.id)
                    context.chat_data[MUTED_KEY] = True
                else:
                    raise e

        return wrapped
    return real_decorator


def fail_with_message(answer_to_message=True):
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            try:
                return func(update, context, *args, **kwargs)
            except Exception as e:
                error_str = str(e)
                logger.error('حدث خطأ أثناء تنفيذ الرد: %s', error_str, exc_info=True)

                error_str_message = f"حدث خطأ أثناء تنفيذ الرد <code>{func.__name__}()</code>: <code>{utilities.escape(error_str)}</code>"
                if answer_to_message and update.message:
                    update.message.reply_html(error_str_message)
                elif answer_to_message and update.callback_query:
                    update.effective_message.reply_html(error_str_message)

                if config.telegram.log_chat:
                    context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

        return wrapped
    return real_decorator


def fail_with_message_job(func):
    @wraps(func)
    def wrapped(context: CallbackContext, *args, **kwargs):
        try:
            return func(context, *args, **kwargs)
        except Exception as e:
            error_str = str(e)
            logger.error('حدث خطأ أثناء تنفيذ المهمة: %s', error_str, exc_info=True)

            error_str_message = f"حدث خطأ أثناء تنفيذ مهمة <code>{func.__name__}()</code>: <code>{utilities.escape(error_str)}</code>"
            if config.telegram.log_chat:
                context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

    return wrapped


def get_secret_santa():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):

            santa = None
            if update.effective_chat.id < 0:
                logger.debug("البحث عن سر سانتا نشط في بيانات الدردشة %d...", update.effective_chat.id)
                if ACTIVE_SECRET_SANTA_KEY in context.chat_data:
                    santa = SecretSanta.from_dict(context.chat_data[ACTIVE_SECRET_SANTA_KEY])
            else:
                if update.callback_query:
                    santa_chat_id = int(context.matches[0].group(1))
                else:
                    santa_chat_id = int(context.matches[0].group(1))

                logger.debug("البحث عن سر سانتا نشط لـ %d في مُعالج الرسائل...", santa_chat_id)
                santa = find_santa_by_chat_id(context.dispatcher.chat_data, santa_chat_id)

            result_santa = func(update, context, santa, *args, **kwargs)
            if result_santa and isinstance(result_santa, SecretSanta):
                logger.debug("حفظ كائن SecretSanta المُرجع للدردشة %d...", result_santa.chat_id)
                context.chat_data[ACTIVE_SECRET_SANTA_KEY] = result_santa.dict()

        return wrapped
    return real_decorator


def gen_participants_list(participants: dict, join_by: Optional[str] = None):
    participants_list = []
    i = 1
    for participant_id, participant in participants.items():
        string = f'<b>{i}</b>. {utilities.mention_escaped_by_id(participant_id, participant["name"])}'
        participants_list.append(string)
        i += 1

    if isinstance(join_by, str):
        return join_by.join(participants_list)

    return participants_list


def cancel_because_cant_send_messages(context: CallbackContext, santa: SecretSanta):
    text = "<i>تم إلغاء هذا السر سانتا لأنني لا أستطيع إرسال الرسائل في هذه المجموعة</i>"
    if santa.get_participants_count():
        participants_list = gen_participants_list(santa.participants, join_by="\n")
        text = f"{text}\nقائمة المشاركين:\n\n{participants_list}"

    return context.bot.edit_message_text(
        chat_id=santa.chat_id,
        message_id=santa.santa_message_id,
        text=text,
        reply_markup=None
    )


def update_secret_santa_message(context: CallbackContext, santa: SecretSanta):
    participants_count = santa.get_participants_count()
    if not participants_count:
        text = EMPTY_SECRET_SANTA_STR
        reply_markup = keyboards.secret_santa(
            santa.chat_id,
            context.bot.username,
            participants_count=participants_count
        )
    elif santa.started:
        participants_list = gen_participants_list(santa.participants)

        base_text = '{santa} لقد بدأ هذا السر سانتا وقد ' \
                    '<a href="{bot_link}">تلقى الجميع مطابقتهم</a>!\n' \
                    'قائمة المشاركين:\n\n' \
                    '{participants}'

        text = base_text.format(
            santa=Emoji.SANTA,
            bot_link=BOT_LINK,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
        )
        reply_markup = None
    else:
        participants_list = gen_participants_list(santa.participants)

        min_participants_text = ""
        if santa.get_missing_count() > 0:
            min_participants_text = f". يحتاج {santa.get_missing_count()} شخص آخر لبدء هذا"

        base_text = '{santa} أوه! سر سانتا جديد!\nقائمة المشاركين:\n\n{participants}\n\n' \
                    'للانضمام، استخدم زر "<b>انضم</b>" أدناه ثم اضغط على "<b>ابدأ</b>".\n' \
                    'فقط {creator} يمكنه بدء هذا السر سانتا{min_participants}'

        text = base_text.format(
            santa=Emoji.SANTA,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
            min_participants=min_participants_text
        )

        reply_markup = keyboards.secret_santa(
            santa.chat_id,
            context.bot.username,
            participants_count=participants_count
        )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except (BadRequest, TelegramError) as e:
        logger.error("استثناء أثناء تعديل رسالة سر سانتا (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


def create_new_secret_santa(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    if santa:
        text_message_exists = f"👆 هناك بالفعل <a href=\"{santa.link()}\">سر سانتا نشط</a> في " \
                              f"هذه الدردشة! " \
                              f"يمكنك أن تطلب من {santa.creator_name_escaped} إلغاءه باستخدام أزرار الرسالة"
        try:
            context.bot.send_message(
                update.effective_chat.id,
                text_message_exists,
                reply_to_message_id=santa.santa_message_id,
                allow_sending_without_reply=False
            )
        except (TelegramError, BadRequest) as e:
            if str(e).lower() != "replied message not found":
                raise e

            update.message.reply_html(f"{Emoji.SANTA} هناك بالفعل سر سانتا نشط في هذه الدردشة! يمكنك أن تطلب من {santa.creator_name_escaped} "
                                      f"(أو مسؤول) إلغاءه باستخدام <code>/cancel</code>")

        return

    new_secret_santa = SecretSanta(
        origin_message_id=update.effective_message.message_id,
        user_id=update.effective_user.id,
        user_name=update.effective_user.first_name,
        chat_id=update.effective_chat.id,
        chat_title=update.effective_chat.title,
    )

    reply_markup = keyboards.secret_santa(update.effective_chat.id, context.bot.username)
    if update.callback_query:
        update.callback_query.edit_message_text(EMPTY_SECRET_SANTA_STR, reply_markup=reply_markup)
        santa_message_id = update.effective_message.message_id
    else:
        sent_message = update.message.reply_html(
            EMPTY_SECRET_SANTA_STR,
            reply_markup=reply_markup
        )
        santa_message_id = sent_message.message_id

    new_secret_santa.santa_message_id = santa_message_id

    return new_secret_santa


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("/newsanta command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if update.message and update.message.sender_chat:
        update.message.reply_html(f"عذراً، لا يُسمح للمستخدمين المجهولين بإنشاء سر سانتا {Emoji.SAD}")
        return

    return create_new_secret_santa(update, context, santa)


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("زر سر سانتا جديد: %d -> %d", update.effective_user.id, update.effective_chat.id)

    return create_new_secret_santa(update, context, santa)


def find_key(dispatcher_user_data: dict, target_chat_id: int, key_to_find: Union[int, str]) -> bool:
    for chat_data_chat_id, chat_data in dispatcher_user_data.items():
        if chat_data_chat_id != target_chat_id:
            continue

        return key_to_find in chat_data


def find_santa_by_chat_id(dispatcher_chat_data: dict, santa_chat_id: int):
    for chat_data_chat_id, chat_data in dispatcher_chat_data.items():
        if chat_data_chat_id != santa_chat_id:
            continue

        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            logger.debug("بيانات الدردشة للدردشة %d موجودة، لكن لا يوجد سر سانتا نشط", santa_chat_id)
            return

        santa_dict = chat_data[ACTIVE_SECRET_SANTA_KEY]
        return SecretSanta.from_dict(santa_dict)


@fail_with_message()
def on_join_deeplink(update: Update, context: CallbackContext):
    santa_chat_id = int(context.matches[0].group(1))
    logger.info("رابط انضمام من %d، معرّف الدردشة: %d", update.effective_user.id, santa_chat_id)

    if find_key(context.dispatcher.chat_data, santa_chat_id, MUTED_KEY):
        update.message.reply_html(f"يبدو أنني لا أستطيع إرسال رسائل في تلك المجموعة. لا أستطيع السماح "
                                  f"للمشاركين الجدد بالانضمام حتى أستطيع إرسال رسائل هناك، عذراً {Emoji.SAD}")
        return

    santa = find_santa_by_chat_id(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        if RECENTLY_LEFT_KEY in context.bot_data and santa_chat_id in context.bot_data[RECENTLY_LEFT_KEY]:
            logger.debug(f"لا يوجد سانتا نشط في {santa_chat_id} والدردشة تظهر في قائمة الدردشات التي غادرتها مؤخراً")
            update.message.reply_html(f"يبدو أنني تم إزالتي من مجموعة سر سانتا هذه {Emoji.SAD}")
        else:
            logger.debug(f"لا يوجد سانتا نشط في {santa_chat_id}")
            update.message.reply_html(f"يبدو أنه لا يوجد سر سانتا نشط في هذه المجموعة {Emoji.SAD} "
                                      f"ربما استخدمت زر \"<b>انضم</b>\" من سر سانتا قديم/غير نشط")
        return

    if config.santa.max_participants and santa.get_participants_count() >= config.santa.max_participants:
        text = f"عذراً، للأسف {santa.inline_link('هذا السر سانتا')} قد بلغ الحد الأقصى من المشاركين {Emoji.SAD}"
        update.message.reply_html(text)
        return

    if santa.is_participant(update.effective_user):
        santa.remove(update.effective_user)

    duplicate_name = santa.is_duplicate_name(update.effective_user.first_name)
    santa.add(update.effective_user)

    context.dispatcher.chat_data[santa_chat_id][ACTIVE_SECRET_SANTA_KEY] = santa.dict()

    if santa.creator_id == update.effective_user.id:
        wait_for_start_text = f"\nيمكنك بدؤه في أي وقت باستخدام زر \"<b>ابدأ المطابقة</b>\" في المجموعة، " \
                              f"عندما ينضم على الأقل {config.santa.min_participants} شخص"
    else:
        wait_for_start_text = f"انتظر الآن حتى يبدأ {santa.creator_name_escaped}"

    reply_markup = keyboards.joined_message(santa_chat_id)
    sent_message = update.message.reply_html(
        f"{Emoji.TREE} لقد انضممت إلى {santa.chat_title_escaped}'s {santa.inline_link('سر سانتا')}!\n"
        f"{wait_for_start_text}. ستتلقى مطابقتك هنا، في هذه الدردشة",
        reply_markup=reply_markup
    )

    if duplicate_name:
        sent_message.reply_html(f"بالمناسبة، يوجد مشارك آخر يحمل الاسم \"{utilities.html_escape(duplicate_name)}\" "
                                f"في هذا السر سانتا. يمكنك تغيير اسمك من إعدادات Telegram الخاصة بك واستخدام "
                                f"زر \"تحديث اسمك\" أعلاه لتجنب الارتباك {Emoji.SNOWMAN_2}", quote=True)

    santa.set_user_join_message_id(update.effective_user, sent_message.message_id)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_leave_button_group(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("زر مغادرة في المجموعة: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa.is_participant(update.effective_user):
        update.callback_query.answer(f"{Emoji.FREEZE} لم تنضم إلى هذا السر سانتا!", show_alert=True)
        return

    last_join_message_id = santa.get_user_join_message_id(update.effective_user)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    update.callback_query.answer(f"لقد تمت إزالتك من هذا السر سانتا")

    logger.debug("إزالة لوحة المفاتيح من آخر رسالة انضمام في الخاصة...")
    context.bot.edit_message_reply_markup(update.effective_user.id, last_join_message_id, reply_markup=None)

    return santa


def save_recently_started_santa(bot_data: dict, santa: SecretSanta):
    chat_id = santa.chat_id

    if RECENTLY_STARTED_SANTAS_KEY not in bot_data:
        bot_data[RECENTLY_STARTED_SANTAS_KEY] = {}
    if chat_id not in bot_data[RECENTLY_STARTED_SANTAS_KEY]:
        bot_data[RECENTLY_STARTED_SANTAS_KEY][chat_id] = {}

    bot_data[RECENTLY_STARTED_SANTAS_KEY][chat_id][santa.santa_message_id] = santa.dict()


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_match_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("زر بدء المطابقة: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} فقط {santa.creator_name} يمكنه استخدام هذا الزر وبدء المطابقة في سر سانتا",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    update.callback_query.answer(f'{Emoji.HOURGLASS} جاري إنشاء المطابقات...', cache_time=5)

    sent_message = update.effective_message.reply_html(f'{Emoji.HOURGLASS} <i>جاري مطابقة المستخدمين...</i>')

    blocked_by = []
    for user_id, user_data in santa.participants.items():
        try:
            context.bot.send_chat_action(user_id, ChatAction.TYPING)
        except (TelegramError, BadRequest) as e:
            if Error.USER_BLOCKED_BOT in str(e).lower():
                logger.debug("%d حظر البوت", user_id)
            else:
                logger.warning("لا يمكن إرسال إجراء الدردشة إلى %d: %s", user_id, str(e))

            blocked_by.append(utilities.mention_escaped_by_id(user_id, user_data["name"]))

    if blocked_by:
        users_list = ", ".join(blocked_by)
        text = f"لا أستطيع بدء سر سانتا لأن بعض المستخدمين ({users_list}) قد حظروني {Emoji.SAD}\n" \
               f"يحتاجون إلى إلغاء حظرني حتى أستطيع إرسال مطابقتهم"
        sent_message.edit_text(text)
        return

    matches = []
    max_attempts = 12
    failed_attempts = 0
    while failed_attempts < max_attempts:
        try:
            matches = utilities.draft(list(santa.participants.keys()))
            break
        except (utilities.TooManyInvalidPicks, utilities.StuckOnLastItem) as e:
            failed_attempts += 1
            logger.warning("خطأ في إعداد الأزواج: %s (محاولة فاشلة %d/%d)", str(e), failed_attempts, max_attempts)

    if not matches:
        logger.error("قائمة المطابقات لا تزال فارغة (محاولات فاشلة: %d/%d)", failed_attempts, max_attempts)

        utilities.log_tg(context.bot, f"#drafting_error أثناء إنشاء الأزواج للدردشة {update.effective_chat.id}")

        text = f"{Emoji.WARN} <i>{update.effective_user.mention_html()}, " \
               f"حدث خطأ أثناء سحب سر سانتا. يرجى المحاولة مرة أخرى</i>"
        sent_message.edit_text(text)
        return

    logger.debug("تم جمع أزواج المطابقات، محاولات فاشلة: %d", failed_attempts)

    for santa_id, present_receiver_id in matches:
        present_receiver_name = santa.get_user_name(present_receiver_id)
        present_receiver_mention = utilities.mention_escaped_by_id(present_receiver_id, present_receiver_name)

        text = f"{Emoji.SANTA}{Emoji.PRESENT} أنت <a href=\"{santa.link()}\">سر سانتا</a> لـ {present_receiver_mention}!"

        match_message = context.bot.send_message(santa_id, text)
        santa.set_user_match_message_id(santa_id, match_message.message_id)

    santa.start()

    logger.debug("إزالة سر سانتا النشط من بيانات الدردشة وحفظ نسخة في بيانات البوت...")
    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    save_recently_started_santa(context.bot_data, santa)

    text = f"لقد تلقى الجميع مطابقتهم في <a href=\"{BOT_LINK}\">الدردشات الخاصة بهم</a>!"
    sent_message.edit_text(text)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("زر الإلغاء: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa:
        logger.warning("زر الإلغاء، لكن لا يوجد سر سانتا نشط في الدردشة")
        update.callback_query.edit_message_text("<i>لم يعد هذا السر سانتا نشطًا</i>", reply_markup=None)
        utilities.log_tg(context.bot, "تم استخدام زر الإلغاء، لكن لا يوجد سر سانتا نشط: تحقق من السجلات!")
        return

    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} فقط {santa.creator_name} يمكنه استخدام هذا الزر. يمكن للمسؤولين استخدام /cancel "
            f"لإلغاء أي سر سانتا نشط",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    text = "<i>تم إلغاء هذا السر سانتا بواسطة منشئه</i>"
    update.callback_query.edit_message_text(text, reply_markup=None)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_revoke_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("زر إلغاء: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} فقط {santa.creator_name} يمكنه استخدام هذا الزر",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    return update.callback_query.answer(
        f"{Emoji.WARN} تم تعليق إمكانية إلغاء المطابقات التي تم إرسالها",
        show_alert=True,
        cache_time=Time.DAY_1
    )


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
def on_hide_commands_command(update: Update, context: CallbackContext):
    logger.debug("/hidecommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=[],
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("تم. قد يستغرق الأمر بعض الوقت لاختفائها. "
                              "يمكنك استخدام <code>/showcommands</code> إذا كنت تريد أن يتمكن مسؤولو المجموعة من "
                              "رؤيتها مرة أخرى")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
def on_show_commands_command(update: Update, context: CallbackContext):
    logger.debug("/showcommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("تم. قد يستغرق الأمر بعض الوقت لظهورها")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("/cancel command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa:
        update.message.reply_html("<i>لا يوجد سر سانتا نشط</i>")
        return

    user_id = update.effective_user.id
    if not santa.creator_id != user_id and user_id not in get_admin_ids(context.bot, update.effective_chat.id):
        logger.debug("المستخدم ليس مسؤولًا ولا منشئ السر سانتا")
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    try:
        context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=santa.santa_message_id,
            text="<i>تم إلغاء هذا السر سانتا بواسطة منشئه أو بواسطة مسؤول</i>",
            reply_markup=None
        )
    except (TelegramError, BadRequest) as e:
        logger.warning("خطأ أثناء تعديل رسالة السر سانتا الملغاة: %s", str(e))
        if Error.MESSAGE_TO_EDIT_NOT_FOUND not in str(e).lower():
            raise e

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="<i>تم إلغاء سر سانتا في هذه الدردشة</i>",
        reply_to_message_id=santa.santa_message_id,
        allow_sending_without_reply=True,
    )


def private_chat_button():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, santa: Optional[SecretSanta], *args, **kwargs):
            santa_chat_id = int(context.matches[0].group(1))
            logger.debug("زر الدردشة الخاصة، معرّف الدردشة: %d", santa_chat_id)

            if not santa:
                logger.debug("المستخدم ضغط على زر الدردشة الخاصة، لكن لا يوجد سر سانتا نشط لتلك الدردشة")
                update.callback_query.answer(f"سر سانتا هذه الدردشة لم يعد صالحاً", show_alert=True)
                update.callback_query.edit_message_reply_markup(reply_markup=None)
                return

            if not santa.is_participant(update.effective_user):
                update.callback_query.answer(f"{Emoji.FREEZE} لم تشارك في هذا السر سانتا!",
                                             show_alert=True)
                update.callback_query.edit_message_reply_markup(reply_markup=None)
                return

            return func(update, context, santa, *args, **kwargs)

        return wrapped
    return real_decorator


@fail_with_message(answer_to_message=True)
@get_secret_santa()
@private_chat_button()
def on_update_name_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("زر تحديث الاسم في الدردشة الخاصة: %d (معرّف دردشة سانتا: %d)", update.effective_user.id, santa.chat_id)

    name = update.effective_user.first_name[:NAME_MAX_LENGTH]
    name_updated = False

    if name != santa.get_user_name(update.effective_user):
        santa.set_user_name(update.effective_user, name)
        name_updated = True

    update.callback_query.answer(f"تم تحديث اسمك إلى: {name}\n\nتتيح لك هذه الخيار تغيير اسمك في Telegram وتحديثه في القائمة "
                                 f"(مفيد إذا كان هناك مشاركون يحملون أسماء مشابهة)", show_alert=True)

    if name_updated:
        try:
            update_secret_santa_message(context, santa)
        except (TelegramError, BadRequest) as e:
            if Error.MESSAGE_NOT_MODIFIED not in e.message:
                raise e
            logger.warning("زر تحديث الاسم في الدردشة الخاصة: لم يتم تعديل رسالة سانتا السر بعد الاستخدام")

        return santa


@fail_with_message(answer_to_message=True)
@get_secret_santa()
@private_chat_button()
def on_leave_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("زر مغادرة في الدردشة الخاصة: %d (معرّف دردشة سانتا: %d)", update.effective_user.id, santa.chat_id)

    santa.remove(update.effective_user)

    text = f"{Emoji.FREEZE} لقد تمت إزالتك من {santa.chat_title_escaped}'s " \
           f"<a href=\"{santa.link()}\">سر سانتا</a>"
    update.callback_query.edit_message_text(text, reply_markup=None)

    try:
        update_secret_santa_message(context, santa)
    except (TelegramError, BadRequest) as e:
        if Error.MESSAGE_NOT_MODIFIED not in e.message:
            raise e
        logger.warning("زر مغادرة في الدردشة الخاصة: لم يتم تعديل رسالة سانتا السر بعد الاستخدام")

    return santa


@fail_with_message(answer_to_message=False)
def on_supergroup_migration(update: Update, context: CallbackContext):
    if not update.message.migrate_to_chat_id:
        return

    logger.info(f"ترحيل السوبرغروب: {update.effective_chat.id} -> {update.message.migrate_to_chat_id}")

    old_chat_id = update.effective_chat.id
    new_chat_id = update.message.migrate_to_chat_id

    if ACTIVE_SECRET_SANTA_KEY not in context.chat_data:
        return

    logger.debug("معرّف الدردشة القديم %d لديه سر سانتا جارٍ", old_chat_id)

    santa_dict = context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY)
    old_santa = SecretSanta.from_dict(santa_dict)

    new_secret_santa = SecretSanta(
        origin_message_id=update.effective_message.message_id,
        user_id=old_santa.creator_id,
        user_name=old_santa.creator_name,
        chat_id=new_chat_id,
        chat_title=update.effective_chat.title,
        participants=old_santa.participants
    )

    logger.debug("إرسال رسالة جديدة...")
    reply_markup = keyboards.secret_santa(new_chat_id, context.bot.username)
    sent_message = context.bot.send_message(new_chat_id, EMPTY_SECRET_SANTA_STR, reply_markup=reply_markup)
    new_secret_santa.santa_message_id = sent_message.message_id

    logger.debug("حفظ بيانات دردشة جديدة للمجموعة السوبرغروب %d...", new_chat_id)
    context.dispatcher.chat_data[new_chat_id] = {ACTIVE_SECRET_SANTA_KEY: new_secret_santa.dict()}

    logger.debug("تحديث الرسالة الجديدة...")
    update_secret_santa_message(context, new_secret_santa)


@fail_with_message(answer_to_message=False)
def on_new_group_chat(update: Update, context: CallbackContext):
    logger.info("دردشة مجموعة جديدة: %d", update.effective_chat.id)

    if config.telegram.exit_unknown_groups and update.effective_user.id not in config.telegram.admins:
        logger.info("غير مصرح: مغادرة...")
        update.effective_chat.leave()
        return

    context.chat_data.pop(REMOVED_KEY, None)

    if RECENTLY_LEFT_KEY in context.bot_data:
        logger.debug("إزالة المجموعة من قائمة الدردشات التي غادرتها مؤخراً...")
        context.bot_data[RECENTLY_LEFT_KEY].pop(update.effective_chat.id, None)

    if not config.santa.start_button_on_new_group:
        return

    text = f"مرحبًا بالجميع! أنا بوت يساعد مجموعات الدردشات في تنظيم " \
           f"سر سانتا {Emoji.SANTA}{Emoji.SHH}\n" \
           f"يمكن لأي شخص استخدام الزر أدناه لبدء واحد جديد. بدلاً من ذلك، يمكن استخدام الأمر <code>/newsanta</code> " \
           f"لبدء واحد جديد"

    update.message.reply_html(
        text,
        reply_markup=keyboards.new_santa(),
        quote=False,
    )


@fail_with_message()
def on_help(update: Update, _):
    logger.info("/start أو /help من: %s (النص: %s)", update.effective_user.id, update.message.text)

    source_code = "https://github.com/zeroone2numeral2/tg-secret-santa-bot"
    text = f"مرحبًا {utilities.html_escape(update.effective_user.first_name)}!" \
           f"\nيمكنني مساعدتك في تنظيم سر سانتا 🤫🎅🏼🎁 في مجموعاتك الدردشات :)\n" \
           f"فقط أضفني إلى دردشة واستخدم <code>/newsanta</code> لبدء سر سانتا جديد." \
           f"\n\nالكود المصدر <a href=\"{source_code}\">هنا</a>"

    update.message.reply_html(text)


@fail_with_message()
@superadmin
def admin_ongoing_command(update: Update, context: CallbackContext):
    logger.info("/ongoing from %d", update.effective_user.id)

    santa_count = 0
    participants_count = 0
    for chat_data_chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa_count += 1
        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])
        participants_count += santa.get_participants_count()

    text = f"• أسر سانتا الجارية: {santa_count} ({participants_count} مشارك)"

    if RECENTLY_STARTED_SANTAS_KEY in context.bot_data:
        recently_started_chats_count = len(context.bot_data[RECENTLY_STARTED_SANTAS_KEY])
        recently_started_santas_count = 0
        for _, santas_data in context.bot_data[RECENTLY_STARTED_SANTAS_KEY].items():
            recently_started_santas_count += len(santas_data)

        text = f"{text}\n• أسر سانتا التي بدأت مؤخرًا: {recently_started_santas_count} في " \
               f"{recently_started_chats_count} مجموعة"

    update.message.reply_html(text)


def allowed(permission: Optional[bool]):
    if permission is None:
        return True

    return permission


def was_muted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if could_send_messages and not can_send_messages:
        return True
    return False


def was_unmuted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if not could_send_messages and can_send_messages:
        return True
    return False


@fail_with_message(answer_to_message=False)
def on_my_chat_member_update(update: Update, context: CallbackContext):
    logger.debug("تحديث العضو في الدردشة %d", update.my_chat_member.chat.id)
    my_chat_member = update.my_chat_member

    if my_chat_member.chat.id > 0:
        if my_chat_member.new_chat_member.status in (ChatMember.LEFT, ChatMember.KICKED):
            logger.debug("تم حظر البوت بواسطة %d (حالة العضو الجديد: %s)", my_chat_member.chat.id, my_chat_member.new_chat_member.status)
            context.user_data[BLOCKED_KEY] = True
        elif my_chat_member.new_chat_member.status == ChatMember.MEMBER:
            logger.debug("تم إلغاء حظر البوت بواسطة %d", my_chat_member.chat.id)
            context.user_data.pop(BLOCKED_KEY, None)
        else:
            logger.debug("لا تغيير ذي صلة حدث (دردشة خاصة): %s", my_chat_member)

        return

    if my_chat_member.new_chat_member.status == ChatMember.LEFT:
        logger.debug("old_chat_member: %s", my_chat_member.old_chat_member)
        logger.debug("new_chat_member: %s", my_chat_member.new_chat_member)
        logger.info("تمت إزالة البوت من %d، يتم إزالة بيانات الدردشة...", my_chat_member.chat.id)
        context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
        context.chat_data.pop(MUTED_KEY, None)

        now = utilities.now()

        context.chat_data[REMOVED_KEY] = now

        if RECENTLY_LEFT_KEY not in context.bot_data:
            context.bot_data[RECENTLY_LEFT_KEY] = {}
        context.bot_data[RECENTLY_LEFT_KEY][my_chat_member.chat.id] = now
    elif was_muted(my_chat_member):
        logger.debug("تم كتم البوت في %d", my_chat_member.chat.id)
        context.chat_data[MUTED_KEY] = True
    elif was_unmuted(my_chat_member):
        logger.debug("تم إلغاء كتم البوت في %d", my_chat_member.chat.id)
        context.chat_data.pop(MUTED_KEY, None)
    else:
        logger.debug("لا تغيير ذي صلة حدث (دردشة جماعية): %s", my_chat_member)


def secret_santa_expired(context: CallbackContext, santa: SecretSanta):
    if not santa.started:
        text = f"<i>انتهت صلاحية هذا السر سانتا ({config.santa.timeout} يوم قد مضى منذ إنشائه)</i>"
    else:
        participants_list = gen_participants_list(santa.participants)
        text = '{hourglass} تم إغلاق هذا السر سانتا. قائمة المشاركين:\n\n{participants}'.format(
            hourglass=Emoji.HOURGLASS,
            participants="\n".join(participants_list)
        )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=None
        )
    except (BadRequest, TelegramError) as e:
        logger.error("استثناء أثناء إغلاق رسالة السر سانتا (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


@fail_with_message_job
def close_old_secret_santas(context: CallbackContext):
    logger.info("وظيفة تنظيف سر سانتا الغير نشط...")

    for chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])

        now = utilities.now()
        diff_seconds = (now - santa.created_on).total_seconds()
        if diff_seconds <= config.santa.timeout * Time.DAY_1:
            continue

        if MUTED_KEY in chat_data:
            logger.info("لا يمكن تعديل رسالة سانتا المنتهية في الدردشة %d: البوت موضح كمكتوم", chat_id)
        else:
            secret_santa_expired(context, santa)

        logger.debug("إزالة سر سانتا من الدردشة %d", chat_id)
        chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    logger.info("...انتهت وظيفة التنظيف")


@fail_with_message_job
def bot_data_cleanup(context: CallbackContext):
    logger.info("تنفيذ وظيفة التنظيف...")

    if RECENTLY_LEFT_KEY in context.bot_data:
        logger.info("تنظيف %s...", RECENTLY_LEFT_KEY)

        chat_ids_to_pop = []
        for chat_id, left_dt in context.dispatcher.bot_data[RECENTLY_LEFT_KEY].items():
            now = utilities.now()
            diff_seconds = (now - left_dt).total_seconds()
            if diff_seconds <= Time.WEEK_4:
                continue

            chat_ids_to_pop.append(chat_id)

        logger.debug("%d دردشات لإزالتها", len(chat_ids_to_pop))
        for chat_id in chat_ids_to_pop:
            logger.debug("إزالة الدردشة %d من قائمة الدردشات التي غادرتها مؤخراً", chat_id)
            context.dispatcher.bot_data[RECENTLY_LEFT_KEY].pop(chat_id, None)

    if RECENTLY_STARTED_SANTAS_KEY in context.bot_data:
        logger.info("تنظيف %s...", RECENTLY_STARTED_SANTAS_KEY)

        chat_ids_to_pop = []
        logger.debug("عدد الدردشات المخزنة حالياً: %d", len(context.bot_data[RECENTLY_STARTED_SANTAS_KEY]))
        for chat_id, chat_santas in context.bot_data[RECENTLY_STARTED_SANTAS_KEY].items():
            santa_ids_to_pop = []
            for santa_message_id, santa_dict in chat_santas.items():
                santa = SecretSanta.from_dict(santa_dict)
                now = utilities.now()
                diff_seconds = (now - santa.started_on).total_seconds()
                if diff_seconds <= Time.WEEK_2:
                    continue

                santa_ids_to_pop.append(santa_message_id)

            logger.debug("%d santa_ids لإزالتها", len(santa_ids_to_pop))
            for santa_id in santa_ids_to_pop:
                logger.debug("إزالة santa_id %d من chat_id %d", santa_id, chat_id)
                chat_santas.pop(santa_id, None)

            if not chat_santas:
                chat_ids_to_pop.append(chat_id)

        logger.debug("%d chat_ids لإزالتها", len(chat_ids_to_pop))
        for chat_id in chat_ids_to_pop:
            logger.debug("إزالة chat_id %d لأن dict الخاص به الآن فارغ", chat_id)
            context.bot_data[RECENTLY_STARTED_SANTAS_KEY].pop(chat_id, None)

    logger.info("...انتهت تنفيذ الوظيفة")


def main():
    dispatcher = updater.dispatcher

    dispatcher.add_handler(MessageHandler(NewGroup(), on_new_group_chat))
    dispatcher.add_handler(MessageHandler(Filters.status_update.migrate, on_supergroup_migration))

    dispatcher.add_handler(CommandHandler(["ongoing"], admin_ongoing_command, filters=Filters.chat_type.private))

    dispatcher.add_handler(MessageHandler(Filters.chat_type.private & Filters.regex(r"^/start (-?\d+)"), on_join_deeplink))
    dispatcher.add_handler(CommandHandler(["start", "help"], on_help, filters=Filters.chat_type.private))

    dispatcher.add_handler(CommandHandler(["new", "newsanta", "santa"], on_new_secret_santa_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["cancel"], on_cancel_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["hidecommands"], on_hide_commands_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["showcommands"], on_show_commands_command, filters=Filters.chat_type.groups))

    dispatcher.add_handler(CallbackQueryHandler(on_new_secret_santa_button, pattern=r'^newsanta$'))
    dispatcher.add_handler(CallbackQueryHandler(on_match_button, pattern=r'^match$'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_group, pattern=r'^leave$'))
    dispatcher.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r'^cancel$'))
    dispatcher.add_handler(CallbackQueryHandler(on_revoke_button, pattern=r'^revoke$'))

    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_private, pattern=r'^private:leave:(-\d+)$'))
    dispatcher.add_handler(CallbackQueryHandler(on_update_name_button_private, pattern=r'^private:updatename:(-\d+)$'))

    dispatcher.add_handler(ChatMemberHandler(on_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    updater.job_queue.run_repeating(close_old_secret_santas, interval=Time.HOUR_6, first=Time.MINUTE_30)
    updater.job_queue.run_repeating(bot_data_cleanup, interval=Time.DAY_1, first=Time.HOUR_6)

    updater.bot.set_my_commands([])  # تأكد من أن البوت ليس لديه أي أمر محدد...
    updater.bot.set_my_commands(  # ...ثم تعيين النطاق للدردشات الخاصة
        commands=Commands.PRIVATE,
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...ثم تعيين النطاق لمديري المجموعة
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query", "my_chat_member"]  
