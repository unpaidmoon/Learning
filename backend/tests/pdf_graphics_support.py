"""Self-authored PDFs with explicit Form transforms and clipping operators."""

import zlib


class GraphicsPDF:
    def __init__(self):
        self.objects = []

    def add(self, body):
        self.objects.append(body.encode("ascii") if isinstance(body, str) else body)
        return len(self.objects)

    def stream(self, attrs, body):
        if isinstance(body, str):
            body = body.encode("ascii")
        return self.add(
            f"<< {attrs} /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream"
        )

    def raster(self, color=(0, 190, 0)):
        return self.stream(
            "/Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceRGB "
            "/BitsPerComponent 8 /Filter /FlateDecode",
            zlib.compress(bytes(color) * 4),
        )

    def form(self, body, resources="", bbox="0 0 100 60", matrix="1 0 0 1 0 0"):
        return self.stream(
            f"/Type /XObject /Subtype /Form /FormType 1 /BBox [{bbox}] "
            f"/Matrix [{matrix}] /Resources << {resources} >>",
            body,
        )

    def write(self, path, body, resources="", *, rotation=0, crop=None, anchor=None):
        font = self.add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        if anchor is None:
            anchor = "BT /F0 12 Tf 50 310 Td (Figure 1: local green diagram.) Tj ET\n"
        contents = self.stream("", anchor + body)
        page = self.add("")
        pages = self.add(f"<< /Type /Pages /Kids [{page} 0 R] /Count 1 >>")
        crop_value = f"/CropBox [{crop}] " if crop else ""
        self.objects[page - 1] = (
            f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 400 400] "
            f"{crop_value}/Rotate {rotation} /Resources << /Font << /F0 {font} 0 R >> "
            f"{resources} >> /Contents {contents} 0 R >>"
        ).encode()
        catalog = self.add(f"<< /Type /Catalog /Pages {pages} 0 R >>")
        data, offsets = bytearray(b"%PDF-1.7\n"), [0]
        for index, obj in enumerate(self.objects, 1):
            offsets.append(len(data))
            data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
        start = len(data)
        data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            data.extend(f"{offset:010} 00000 n \n".encode())
        data.extend(
            f"trailer\n<< /Size {len(offsets)} /Root {catalog} 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n".encode()
        )
        path.write_bytes(data)
        return path


def clipped_form_pdf(path, *, fractional=False, nested=False, **kwargs):
    pdf = GraphicsPDF()
    image = pdf.raster()
    form = pdf.form(
        "q 100 0 0 100 0 0 cm /Im Do Q",
        f"/XObject << /Im {image} 0 R >>",
        bbox=".21 .24 40.32 30.41" if fractional else "0 0 40 30",
        matrix="1 0 0 1 80 240",
    )
    if nested:
        form = pdf.form(
            "/Inner Do",
            f"/XObject << /Inner {form} 0 R >>",
            bbox="0 0 200 350",
            matrix="1.2 0 0 .8 20 30",
        )
    neighbor = "200 270" if nested else "150 280"
    return pdf.write(
        path,
        f"/Fm Do\n1 0 0 rg {neighbor} 20 20 re f",
        f"/XObject << /Fm {form} 0 R >>",
        **kwargs,
    )
