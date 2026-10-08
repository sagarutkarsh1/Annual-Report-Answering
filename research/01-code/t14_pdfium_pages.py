"""Does overriding LocalAPI._extract_page_texts with pdfium text work (clean spacing)? Mocked LLM, no network."""
import os, json, shutil, time, glob
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["OPENAI_API_KEY"] = "sk-test"


def pdfium_page_texts(file_path: str) -> list[str]:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(file_path)
    try:
        out = []
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            out.append(tp.get_text_range().replace("\r\n", "\n").replace("\r", "\n"))
            tp.close()
            page.close()
        return out
    finally:
        doc.close()


def main():
    import pageindex.utils as U
    import pageindex.tree_optimize as T
    from pageindex.local_api import LocalAPI

    async def fake_a(model, prompt):
        return '{"subsections": []}' if "splitting an over-long section" in prompt else json.dumps({"summary": "mock"})

    def fake_s(model, prompt, chat_history=None, return_finish_reason=False):
        return ("mock desc", "finished") if return_finish_reason else "mock desc"
    U.llm_acompletion = fake_a
    T.llm_acompletion = fake_a
    U.llm_completion = fake_s
    LocalAPI._extract_page_texts = staticmethod(pdfium_page_texts)   # <-- the override
    from pageindex import PageIndexClient
    store = os.path.join(HERE, "store_pdfium")
    shutil.rmtree(store, ignore_errors=True)
    client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": store}, chat="gpt-5.6-sol")
    pdf = r"C:\Users\ayush\AppData\Local\Temp\claude\C--Users-ayush-Annual-Report-Answering\536cb16d-2d66-4e30-9660-8cbaf6c92128\scratchpad\pi-oss\examples\documents\2023-annual-report.pdf"
    t = time.time()
    t0 = time.time(); pdfium_page_texts(pdf); print("pdfium text extraction of 222 pages:", round(time.time() - t0, 2), "s")
    res = client.submit_document(pdf)
    print("submit ok", res, round(time.time() - t, 1), "s")
    p = client.get_page_content(res["doc_id"], "10")[0]["markdown"]
    print("page 10 head:", repr(p[:200]))


if __name__ == "__main__":
    main()
