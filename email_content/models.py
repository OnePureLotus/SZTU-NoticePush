from dataclasses import dataclass, field


@dataclass
class InlineImage:
    cid: str
    data: bytes
    mime_subtype: str = "png"


@dataclass
class FileAttachment:
    filename: str
    data: bytes
    mime_type: str = "application/octet-stream"


@dataclass
class DigestEmail:
    subject: str
    html: str
    inline_images: list[InlineImage] = field(default_factory=list)
    file_attachments: list[FileAttachment] = field(default_factory=list)
