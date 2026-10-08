import os, json, sys
os.environ["OPENAI_API_KEY"]="sk-test"
os.environ["OPENAI_BASE_URL"]="http://127.0.0.1:8765/v1"
def main():
    from pageindex import PageIndexClient
    store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store_mock")
    model = sys.argv[1] if len(sys.argv)>1 else "gpt-5.6-sol"
    client = PageIndexClient(index={"model": "gpt-5.6-luna", "storage_path": store}, chat=model)
    doc_id = client.list_documents()["documents"][0]["id"]
    print("doc_id", doc_id)
    try:
        ans = client.chat("What happened to inflation?", doc_id=doc_id, citations=True)
        print("ANSWER:", repr(ans))
    except Exception as e:
        print("ERR", type(e).__name__, str(e)[:500])
    try:
        st = client.chat("What happened to inflation?", doc_id=doc_id, citations=True, stream=True)
        for ev in st.events:
            e = dict(ev)
            if e["type"]=="tool_result": e["output"]=str(e["output"])[:300]+"..."
            print("EVENT", json.dumps(e, ensure_ascii=False)[:420])
    except Exception as e:
        print("ERR2", type(e).__name__, str(e)[:500])
if __name__=="__main__":
    main()
