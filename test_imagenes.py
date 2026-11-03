"""Adjuntar imágenes (OCR + visión) y la opción «sin razonar». Sin red: arma los pedidos y los revisa."""
import json
import os
import tempfile

import agent
import dsapi
import ocr
import providers

fallas = []


def check(nombre, ok, det=""):
    print(("OK    " if ok else "FALLA ") + nombre + ("" if ok else f"  [{det}]"))
    if not ok:
        fallas.append(nombre)


d = tempfile.mkdtemp()
png = os.path.join(d, "x.png")
with open(png, "wb") as f:
    f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
dsapi.check_attachment(png)
check("una imagen se puede adjuntar (antes: «archivo binario»)", True)
txt = dsapi.build_message("hola", [png], tesseract=ocr.find_tesseract())
check("la imagen entra como texto de OCR", "x.png.ocr.txt" in txt and "OCR" in txt, txt[-200:])
imgs = dsapi.image_payloads([png, __file__])
check("solo las imágenes van como imagen", [i["name"] for i in imgs] == ["x.png"], imgs)
um = {"role": "user", "content": "mirá", "_images": [{"name": "a.png", "mime": "image/png", "data": "QUJD"}]}

so = providers.with_no_think({"id": "claude:claude-sonnet-5-5", "provider": "anthropic", "efforts": ["high"], "adaptive": True})
s = providers.make_stream(so, "k", agent.api_messages([um], keep=("_blocks", "_images")), "sin razonar", 100)
check("Claude sin razonar: sin 'thinking'", "thinking" not in s._body)
check("Claude recibe la imagen", s._body["messages"][0]["content"][0]["type"] == "image")
s = providers.make_stream(so, "k", agent.api_messages([um], keep=("_blocks", "_images")), "high", 100)
check("CONTROL: Claude con esfuerzo sí razona", "thinking" in s._body)

oa = {"id": "openai:gpt-5", "provider": "openai", "efforts": ["low"]}
s = providers.make_stream(oa, "k", agent.api_messages([um], keep=("_images",)), "", 100)
check("OpenAI recibe la imagen", json.loads(s._req.data)["messages"][0]["content"][1]["type"] == "image_url")
check("OpenAI no ofrece «sin razonar»", providers.NO_THINK not in providers.with_no_think(oa)["efforts"])

ds = providers.with_no_think({"id": "deepseek-flash", "provider": "deepseek", "efforts": ["low"]})
b = json.loads(providers.make_stream(ds, "k", agent.api_messages([um]), "sin razonar", 100)._req.data)
check("DeepSeek sin razonar: thinking disabled, sin reasoning_effort",
      b.get("thinking") == {"type": "disabled"} and "reasoning_effort" not in b, b)
check("DeepSeek no recibe la imagen (solo el OCR)", b["messages"][0]["content"] == "mirá", b["messages"])
b = json.loads(providers.make_stream(ds, "k", [um], "low", 100)._req.data)
check("CONTROL: DeepSeek con esfuerzo no apaga el razonamiento", "thinking" not in b and b["reasoning_effort"] == "low", b)

print("FALLAS:", fallas or "ninguna")
raise SystemExit(1 if fallas else 0)
