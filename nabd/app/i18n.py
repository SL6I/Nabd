"""Arabic wording for the Arabic postmortem."""

NODE_AR = {
    "gateway": "البوابة", "auth": "خدمة الدخول", "inventory": "خدمة المخزون", "orders": "خدمة الطلبات",
    "payments": "خدمة الدفع", "psp": "مزوّد الدفع", "redis": "ريديس", "postgres": "قاعدة البيانات",
    "orders-db-link": "خط الشبكة بين الطلبات وقاعدة البيانات", "traffic": "المتسوقين",
    "events": "طابور رسائل الطلبات", "fulfillment": "خدمة الشحن", "carrier": "شركة الشحن",
    "catalog-sync": "مهمة مزامنة الأسعار", "supplier": "كتالوج المورّد",
}

CATEGORY_AR = {
    "cache_memory_exhaustion": "امتلاء ذاكرة الكاش بسبب تسريب مفاتيح",
    "bad_config_deploy": "تحديث إعدادات خاطئ",
    "network_degradation": "بطء في خط الشبكة",
    "process_crash": "توقف الخدمة",
    "cpu_saturation": "استهلاك كامل للمعالج من مهمة خلفية",
    "memory_leak": "تسريب ذاكرة",
    "db_connection_exhaustion": "استنزاف اتصالات قاعدة البيانات",
    "db_lock_contention": "قفل على جدول في قاعدة البيانات",
    "dependency_failure": "عطل في خدمة معتمدة",
    "poison_message": "رسالة عالقة توقف الطابور كامل",
    "silent_data_loss": "ضياع بيانات بصمت",
    "orphaned_payments": "مبالغ انخصمت بدون طلبات",
    "lost_events": "رسائل طلبات ضاعت قبل الشحن",
    "corrupted_data": "بيانات تخرّبت",
    "other": "سبب غير مصنّف",
}

PREVENTION = {
    "cache_memory_exhaustion": (
        "Give every temporary key a TTL, alert at 80% of maxmemory, and move bulk dumps out of the shared Redis.",
        "حطّوا مدة صلاحية لكل مفتاح مؤقت، وتنبيه عند ٨٠٪ من الذاكرة، وانقلوا التفريغات الكبيرة من ريديس المشترك."),
    "bad_config_deploy": (
        "Validate config changes against observed dependency latency in CI, and canary config deploys.",
        "تحققوا من تغييرات الإعدادات مقابل زمن الاستجابة الفعلي قبل النشر، وانشروها تدريجياً."),
    "network_degradation": (
        "Ask the network team to repair the primary link, then fail back; add a synthetic probe per link.",
        "بلّغوا فريق الشبكة يصلح الخط الأساسي ثم ارجعوا له، وأضيفوا فحص دوري لكل خط."),
    "process_crash": (
        "Add a liveness probe with automatic restart and find the cause of the kill.",
        "أضيفوا فحص حياة مع إعادة تشغيل تلقائية، وابحثوا عن سبب الإيقاف."),
    "cpu_saturation": (
        "Run reindex jobs in a separate worker with CPU limits, outside the request path.",
        "شغّلوا مهام الفهرسة في عامل منفصل بحدود معالج، بعيد عن مسار الطلبات."),
    "memory_leak": (
        "Bound the response cache, add a heap profile to the next release, and keep the OOM forecast alert.",
        "حدّدوا حجم الكاش، وأضيفوا تحليل للذاكرة في الإصدار القادم، وخلّوا تنبيه التوقع شغّال."),
    "db_connection_exhaustion": (
        "Give batch jobs their own database role with a connection limit and route them to a replica.",
        "اعطوا مهام التقارير مستخدم خاص بحد اتصالات، ووجّهوها لنسخة قراءة."),
    "db_lock_contention": (
        "Run migrations with lock_timeout and in off-peak windows; never hold ACCESS EXCLUSIVE in a long transaction.",
        "شغّلوا الترحيل مع مهلة للقفل وفي وقت هادئ، ولا تمسكوا قفل حصري في معاملة طويلة."),
    "poison_message": (
        "Put a timeout on every call to an outside system, and give the worker a per-message deadline that "
        "dead-letters a message instead of waiting on it forever.",
        "حطّوا مهلة لكل اتصال بنظام خارجي، وحد زمني لكل رسالة يحوّلها لطابور الرسائل الميتة بدل الانتظار للأبد."),
    "silent_data_loss": (
        "Make the invariant a test: a load test must end with every payment matched to an order.",
        "خلّوا القاعدة اختبار: أي اختبار ضغط لازم ينتهي وكل مبلغ مدفوع له طلب."),
    "orphaned_payments": (
        "Write the order before capturing the payment (or in one saga with a compensation step), so no charge can "
        "exist without an order.",
        "اكتبوا الطلب قبل خصم المبلغ، أو في عملية واحدة لها خطوة تعويض، عشان ما يصير خصم بدون طلب."),
    "corrupted_data": (
        "Never let a job rewrite a value from its own previous output; sync from the source of truth and check "
        "the result against it before committing.",
        "لا تخلّون مهمة تعيد كتابة قيمة من مخرجاتها السابقة؛ زامنوا من المصدر الأصلي وقارنوا النتيجة فيه قبل الحفظ."),
    "lost_events": (
        "Never trim a work queue to cap memory; apply back-pressure or spill to disk, and alert on conservation.",
        "لا تقصّون طابور شغل عشان توفرون ذاكرة؛ خففوا الضغط أو احفظوا على القرص، وراقبوا ميزان الرسائل."),
}


def action_ar(name: str, params: dict) -> str:
    if name == "rollback_config":
        return f"أرجّع إعدادات {NODE_AR.get(params.get('service'), params.get('service'))} للنسخة السابقة"
    if name == "restart_service":
        return f"أعيد تشغيل {NODE_AR.get(params.get('service'), params.get('service'))}"
    if name == "start_service":
        return f"أشغّل {NODE_AR.get(params.get('service'), params.get('service'))}"
    if name == "flush_redis_keys":
        return f"أحذف المفاتيح المسرّبة تحت {params.get('prefix')}"
    if name == "switch_db_route":
        return "أحوّل خدمة الطلبات على الخط الاحتياطي لقاعدة البيانات"
    if name == "terminate_db_sessions":
        return f"أقطع جلسات {params.get('application_name')} في قاعدة البيانات"
    if name == "replay_orphan_payments":
        return "أعيد إنشاء طلبات العملاء اللي دفعوا وقلنا لهم إن طلبهم تم"
    if name == "refund_failed_payments":
        return "أرجّع فلوس العملاء اللي انخصم منهم والطلب فشل"
    if name == "replay_lost_events":
        return "أعيد إرسال رسائل الطلبات اللي ضاعت للشحن"
    if name == "quarantine_stuck_message":
        return "أعزل الرسالة العالقة وأعيد تشغيل خدمة الشحن"
    if name == "restore_catalog_prices":
        return "أرجّع الأسعار من كتالوج المورّد"
    return name


STATUS_AR = {"done": "تم", "failed": "فشل", "rejected": "رُفض", "blocked": "منعه حارس الأمان",
             "reverted": "تم التراجع عنه", "proposed": "مقترح"}


def action_past_ar(name: str, params: dict) -> str:
    """The same actions, phrased for a report written after the fact."""
    target = NODE_AR.get(params.get("service"), params.get("service"))
    return {
        "rollback_config": f"إرجاع إعدادات {target} للنسخة السابقة",
        "restart_service": f"إعادة تشغيل {target}",
        "start_service": f"تشغيل {target}",
        "flush_redis_keys": f"حذف المفاتيح المسرّبة تحت {params.get('prefix')}",
        "switch_db_route": "تحويل خدمة الطلبات للخط الاحتياطي لقاعدة البيانات",
        "terminate_db_sessions": f"قطع جلسات {params.get('application_name')} في قاعدة البيانات",
        "replay_orphan_payments": "إعادة إنشاء الطلبات المدفوعة اللي اختفت",
        "refund_failed_payments": "استرجاع مبالغ الطلبات اللي فشلت بعد الخصم",
        "replay_lost_events": "إعادة إرسال رسائل الطلبات الضائعة",
        "quarantine_stuck_message": "عزل الرسالة العالقة في طابور الرسائل الميتة",
        "restore_catalog_prices": "استرجاع الأسعار من كتالوج المورّد",
    }.get(name, name)
