import os, json
os.environ["OPENAI_API_KEY"] = "sk-test"
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:8766/v1"


def main():
    from pageindex import PageIndexClient
    store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store_mock")
    client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": store}, chat="gpt-5.6-sol")
    doc_id = client.list_documents()["documents"][0]["id"]
    r = client.chat("What was core PCE inflation?", doc_id=doc_id, citations=True, protocol="responses", reasoning_effort="low")
    print("TOP KEYS", list(r.keys()))
    print("status", r["status"], "usage", r["usage"])
    print("output types", [o["type"] for o in r["output"]])
    print("items types", [i.get("type") or i.get("role") for i in r["items"]])
    for it in r["items"]:
        t = it.get("type")
        if t == "function_call_output":
            print("function_call_output keys", list(it.keys()), "| output type", type(it["output"]).__name__, "|", str(it["output"])[:240])
        if t == "function_call":
            print("function_call", {k: it[k] for k in ("name", "arguments", "call_id")})
    print("final text:", [c["text"] for o in r["output"] if o["type"] == "message" for c in o["content"]])


if __name__ == "__main__":
    main()
