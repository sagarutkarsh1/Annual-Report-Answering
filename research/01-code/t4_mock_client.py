"""Offline end-to-end indexing through PageIndexClient with the LLM mocked (no network, no money)."""
import os, sys, json, time, asyncio, collections, shutil
os.environ["OPENAI_API_KEY"] = "sk-test-not-real"   # never used: LLM is mocked
def main():
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    import pageindex.utils as U
    import pageindex.tree_optimize as T
    calls = collections.Counter(); in_tok = collections.Counter(); samples = {}
    def kind(prompt):
        if "splitting an over-long section" in prompt: return "expand"
        if "Opening Text" in prompt and "Subsection Titles and Summaries" in prompt: return "summary_parent"
        if "You are given a text chunk from a document" in prompt: return "summary_leaf"
        if "one-sentence description" in prompt: return "doc_description"
        return "other"
    def rec(prompt):
        k = kind(prompt); calls[k]+=1; in_tok[k]+=len(enc.encode(prompt)); samples.setdefault(k, prompt[:1500])
        return k
    async def fake_a(model, prompt):
        k = rec(prompt)
        if k == "expand": return '{"subsections": []}'
        return json.dumps({"summary": "MOCK SUMMARY " + ("x " * 100)})
    def fake_s(model, prompt, chat_history=None, return_finish_reason=False):
        k = rec(prompt)
        out = "MOCK document description."
        return (out, "finished") if return_finish_reason else out
    U.llm_acompletion = fake_a; T.llm_acompletion = fake_a; U.llm_completion = fake_s
    from pageindex import PageIndexClient
    store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store_mock")
    shutil.rmtree(store, ignore_errors=True)
    client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": store}, chat="gpt-5.6-sol")
    print("client models:", client.index_model, client.summary_model, client.chat_model)
    pdf = r"C:\Users\ayush\AppData\Local\Temp\claude\C--Users-ayush-Annual-Report-Answering\536cb16d-2d66-4e30-9660-8cbaf6c92128\scratchpad\pi-oss\examples\documents\2023-annual-report.pdf"
    t = time.time()
    res = client.submit_document(pdf)
    print("submit_document ->", res, "elapsed", round(time.time()-t,1))
    print("LLM calls:", dict(calls)); print("approx input tokens:", dict(in_tok), "TOTAL", sum(in_tok.values()))
    doc = client.get_document(res["doc_id"]); print("get_document:", json.dumps(doc, indent=1)[:700])
    tree = client.get_tree(res["doc_id"], node_summary=True, include_text=False)
    print("get_tree keys:", list(tree.keys()), "| status", tree["status"], "retrieval_ready", tree["retrieval_ready"])
    print("first node:", json.dumps(tree["result"][0], indent=1)[:600])
    print("a parent node keys:", next(list(n.keys()) for n in tree["result"] if n.get("nodes")))
    print("stored files:")
    for d,_,fs in os.walk(store):
        for f in fs:
            p=os.path.join(d,f); print("  ", os.path.relpath(p,store), os.path.getsize(p))
    pages = client.get_page_content(res["doc_id"], "10-11")
    print("get_page_content ->", [ (p['page_index'], list(p.keys()), len(p['markdown'])) for p in pages])
    print("page 10 text head:", repr(pages[0]['markdown'][:300]))
    print("--- SAMPLE LEAF PROMPT ---\n", samples.get("summary_leaf","")[:1200])
    print("--- SAMPLE PARENT PROMPT ---\n", samples.get("summary_parent","")[:1200])
if __name__ == "__main__":
    main()
