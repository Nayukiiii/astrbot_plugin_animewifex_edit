from __future__ import annotations


class ReviewStatus:
    NEED_SOURCE = "need_source"
    PENDING = "pending"
    APPROVED = "approved"
    IMAGE_READY = "image_ready"
    PR_CREATED = "pr_created"
    ONLINE = "online"
    REJECTED = "rejected"

    OPEN = {NEED_SOURCE, PENDING}
    DONE = {ONLINE, REJECTED}
    LOCKED = {APPROVED, IMAGE_READY, PR_CREATED, ONLINE}

    LABELS = {
        NEED_SOURCE: "待补来源",
        PENDING: "待审核",
        APPROVED: "已通过，待选图",
        IMAGE_READY: "图片待确认",
        PR_CREATED: "PR 已创建，待上线",
        ONLINE: "已上线",
        REJECTED: "未通过",
    }

    @classmethod
    def label(cls, status: str) -> str:
        return cls.LABELS.get(status, status or cls.PENDING)
