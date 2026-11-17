import streamlit as st
from pathlib import Path

from kb_engine import(
    cross_encoder_score,
    SimpleVectorStore,
    two_stage_retrieve,
    load_kb
)

st.set_page_config(page_title="RAG两阶段检索演示",layout="wide")  #layout="wide"：宽屏模式
st.title("RAG|Bi-Encoder召回+Cross-Encoder重排演示系统")   #页面顶部最大标题
KB_DIR = Path(__file__).resolve().parent / "kb_documents"

@st.cache_resource   #Streamlit 缓存装饰器
def load_knowledge_base():
    """加载知识库，构建向量库"""
    store=SimpleVectorStore(dim=128)
    #加载文档
    chunk_list,stat_info=load_kb(
        root_dir=str(KB_DIR),
        chunk_size=256,
        overlap=20,
        split_strategy="sentence"
    )
    #灌入向量库
    store.add(
        texts=[c["text"]for c in chunk_list],
        metadatas=[c["meta"]for c in chunk_list],
    )
    return store,chunk_list,stat_info   #stat_info:知识库统计信息字典

store,all_chunks,kb_stat=load_knowledge_base()    #分别接收store,chunk_list,stat_info 
#拆分tab
tab1,tab2,tab3=st.tabs(["聊天界面","知识库统计面板","检索过程可视化"])  #st.tabs：生成三个标签页，点击切换，对应三个模块。

with tab1:
    st.header("问答检索(自动带应用标注)")  #小标题
    query=st.text_input("请输入你的问题：",placeholder="例如：MySQL主从切换步骤是什么")    #st.text_input：单行输入框
    col1,col2=st.columns([1,1])  #分成左右两列，放入数字输入框,宽度比例1：1
    with col1:
        recall_k=st.number_input("Bi粗召回数量 recall_k",min_value=3,max_value=50,value=20)  #上限、下限、默认值
    with col2:
        final_k=st.number_input("最终返回TopK", min_value=1, max_value=5, value=3)
    if st.button("发起检索&生成回答"):
        if not query.strip():   #query.strip()防止空输入
            st.warning("请先输入问题，再点击检索。")
        else:
            recall_k,final_k=int(recall_k),int(final_k)
            if final_k>recall_k:
                st.caption(
                    f"recall_k={recall_k}小于final_k={final_k},"
                    f"最终最多只能返回{recall_k}条。"
                )
                final_k=recall_k
        res=two_stage_retrieve(query,store,recall_k=int(recall_k),final_k=int(final_k))
        #拼接文本，自动带上[1][2]
        answer_text=""
        if len(res)==0:
            answer_text="未检索到相关片段！"
        else:

            #不调用LLM版本
            
            answer_text=f"基于检索到的{len(res)}条片段生成回答： \n"
            for item in res:
                ref=item["ref_id"]
                domain=item["metadata"]["domain"]
                answer_text+=f"- {domain} 相关内容参考 {ref} \n"
        st.subheader("回答结果")  #二级小标题
        st.markdown(answer_text)
        st.divider()   #画一条水平分割线
        st.subheader("应用来源溯源(校验)")   
        st.info("下方为回答中[1][2]对应的原始chunk,可核对引用是否匹配")  #蓝色提示框
        for item in res:
            ref_tag=item["ref_id"]
            #st.expander：可折叠面板
            meta=item["metadata"]
            with st.expander(
                f"引用{ref_tag}|{meta['domain']}|chunk_id:{meta['chunk_id']}"
                f"|文件:{meta['source_file']}"
                ):
                st.write(f"doc_id:{item['id']}")
                st.write(f"Bi相似度:{item['similarity']:.4f}")
                st.write(f"Rerank打分：{item['rerank_score']:.4f}")
                st.text_area("Chunk原文",value=item["document"],height=200,key=f"answer_chunk_{item['id']}")    #st.text_area:文本框，展示chunk文本，固定框高200，长文本可以滚动看
with tab2:
    st.header("知识库统计&Chunk浏览")
    c1,c2,c3=st.columns(3)  #并排创建多列容器
    with c1:
        st.metric("总文档数量",kb_stat["doc_count"])   #指标卡片
    with c2:
        st.metric("总Chunk数量",kb_stat["chunk_count"])
    with c3:
        st.metric("领域种类",kb_stat["domain_count"])
    st.divider()
    st.subheader("各领域Chunk分布")
    domain_count=kb_stat["domain_counter"]
    st.bar_chart(domain_count)  #根据字典画出柱状图
    domain_list=list(domain_count.keys())
    selected_domain=st.selectbox("选择领域过滤查看Chunk",options=["全部"]+domain_list)    #下拉选择框
    filter_chunks=[]
    if selected_domain=="全部":
        filter_chunks=all_chunks
    else:
        filter_chunks=[c for c in all_chunks if c["meta"]["domain"]==selected_domain]
    st.write(f"筛选后共{len(filter_chunks)}个chunk")
    page_size=st.selectbox("每页显示数量",[20,50,100],key="chunk_page_size") #下拉选择框
    page_count=max(1,(len(filter_chunks)+page_size-1)//page_size)  #(a+b-1)//b向上取整
    page=st.selectbox(
        "页码",list(range(1,page_count+1)),
        key=f"chunk_page_{selected_domain}_{page_size}",
    )

    start=(page-1)*page_size
    page_chunks=filter_chunks[start:start+page_size]
    st.caption(f"共{len(filter_chunks)}个chunk,第{page}/{page_count}页")
    for offset ,chunk in enumerate(page_chunks):
        meta=chunk["meta"]
        with st.expander(f"{meta['domain']}|{meta['chunk_id']}|{meta['source_file']}"):
            st.text_area(
                "文本",value=chunk["text"],height=150,
                key=f"kb_chunk_{start+offset}",
            )

with tab3:
    st.header("检索过程可视化：Bi召回——>Rerank重排")
    query_viz=st.text_input("输入查询问题",placeholder="MySQL锁等待如何排查？",key="viz_query")
    recall_k_viz=st.slider("Bi召回候选数量",min_value=5,max_value=20,value=20)
    final_k_viz=st.slider("最终高亮数量",min_value=1,max_value=5,value=3)
    if st.button("执行检索可视化")and query_viz.strip():
        bi_candidates=store.query(query_viz,top_k=recall_k_viz)
        for c in bi_candidates:
            c["rerank_score"]=cross_encoder_score(query_viz,c["document"])
        bi_sorted=sorted(bi_candidates,key=lambda x:x["similarity"],reverse=True)
        rerank_sorted=sorted(bi_candidates,key=lambda x:x["rerank_score"],reverse=True)
        top3_final=rerank_sorted[:final_k_viz]
        top3_ids=set([x["id"]for x in top3_final])



        st.subheader(f"阶段1：Bi-Encoder粗召回Top{len(bi_sorted)}(按向量相似度降序)")
        for idx,item in enumerate(bi_sorted,start=1):
            meta=item["metadata"]
            highlight="【最终选中】"if item ["id"]in top3_ids else""            
            with st.expander(
                f"Bi排名{idx}{highlight}|{meta['domain']}|"
                f"{meta['source_file']}|chunk_id:{meta['chunk_id']}"
            ):
                st.write(f"Bi相似度：{item['similarity']:.4f}")
                st.write(f"Rerank预打分：{item['rerank_score']:.4f}")
                st.text_area("文本",item["document"],height=120,key=f"viz_bi_{item['id']}")

        st.subheader("阶段2：Cross-Encoder Rerank重排序结果(按rerank_score降序)")
        for idx,item in enumerate(rerank_sorted,start=1):
            meta=item["metadata"]
            highlight="【最终选中】"if item ["id"]in top3_ids else""
            with st.expander(
                f"Rerank排名{idx}{highlight}|{meta['domain']}|"
                f"{meta['source_file']}|chunk_id:{meta['chunk_id']}"
            ):
                st.write(f"Bi相似度：{item['similarity']:.4f}")
                st.write(f"Rerank打分：{item['rerank_score']:.4f}")
                st.text_area("文本",item["document"],height=120,key=f"viz_rr_{item['id']}")
        st.divider()
        st.success("对比观察：部分Bi相似度低的片段，经过cross重排后排名上升，就是rerank抢救效果")
                             


        