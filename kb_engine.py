from __future__ import annotations
import os
from functools import lru_cache
from langfuse import observe, get_client

import numpy as np
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from sentence_transformers import SentenceTransformer

EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"   # 512 维，中文，CPU 可跑
EMBED_DIM = 512
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

def load_kb(root_dir:str,chunk_size=300,overlap=50,split_strategy="sentence")->tuple[list[dict],dict]:
    doc_cnt,chunk_cnt=0,0
    all_chunks=[]
    domain_counter={}
      
    for domain in sorted(os.listdir(root_dir)):#os.listdir(root_dir)：列出 root_dir（kb_documents）下面所有的文件名、文件夹名
        d_path=os.path.join(root_dir,domain)  #> 把 kb_documents+mysql拼成完整路径kb_documents/mysql，存入 d_path
        if not os.path.isdir(d_path):continue
        for fn in sorted(os.listdir(d_path)):#列出里面所有文件
            if not fn.endswith((".md",".txt")):continue  #后缀不是这两个，continue 跳过
            fp=os.path.join(d_path,fn)    #拼接完整文件路径：kb_documents/mysql/mysql_连接故障.md
            try:
                with open(fp,"r",encoding="utf-8") as f: #只读模式，with open(...) as f：上下文管理器，读完自动关闭文件，不用手动 f.close ()
                    txt=f.read().strip()  #f.read():一次性读取整篇文章
            except Exception as e:
                print(f"读取失败{fp}|{e}")
                continue
            if not txt:continue  #如果 txt 是空字符串
            doc_cnt+=1
            chunks=chunk_text(
                txt,
                chunk_size=chunk_size,
                overlap=overlap,
                split_strategy=split_strategy,
                )
            if not chunks:continue
            for c in chunks:
                all_chunks.append({
                    "text":c["text"],
                    "meta":{
                        "domain":domain,
                        "chunk_id":c["chunk_id"],
                        "source_file":fn
                    }
                })
            domain_counter[domain]=domain_counter.get(domain,0)+len(chunks)
            chunk_cnt+=len(chunks)
            print(f"[{domain}]{fn}->{len(chunks)}chunks")
    stat_info={
        "doc_count":doc_cnt,
        "chunk_count":chunk_cnt,
        "domain_count":len(domain_counter),
        "domain_counter":domain_counter
    }
    print(f"加载完成：文档={doc_cnt},chunk={chunk_cnt}")
    return all_chunks,stat_info
def chunk_text(text,chunk_size=300,overlap=50,split_strategy="sentence")->list[dict]:
    """支持三种切分方法fixed / sentence / paragraph"""
    chunks=[]
    start=0
    idx=0   #chunkid:chunk-000
    text_len=len(text)
    while start <text_len:
        end=min(start+chunk_size,text_len)
        if end >=text_len:
            end=text_len

        if split_strategy=="fixed":
            #固定长度：不找边界，直接截断
            final_end=end  
        elif split_strategy=="sentence":
            #句子边界：从end往左找最近的句号
            pos=text.rfind("。",start+chunk_size//2,end)
            if pos>start:
                final_end=pos+1
            else:
                final_end=end
        elif split_strategy=="paragraph":
            # 段落边界：从end往左找空行 \n\n
            pos=text.rfind("\n\n",start+chunk_size//2,end)
            if pos>start:
                final_end=pos+2
            else:
                final_end=end
        else:
            final_end=end
        chunk=text[start:final_end].strip()
        if chunk:
            chunks.append({
                "text":chunk,
                "chunk_id":f"chunk-{idx:03d}"
            })
            idx+=1
        #滑动窗口 +防止死循环
        next_start=final_end-overlap
        if next_start<=start:
            next_start=final_end
        start=next_start
    return chunks

@lru_cache(maxsize=1)
def _get_embedder() -> SentenceTransformer:
    #第一次调用会下载模型，之后本地缓存
    return SentenceTransformer(EMBED_MODEL_NAME)

@observe(name="bge_embed", as_type="embedding",
        capture_input=False, capture_output=False)
def embed_texts(texts:list[str])->np.ndarray:
    "批量编码文档，返回(n,512)的归一化向量"
    model=_get_embedder()
    vecs=model.encode(
        texts,
        batch_size=32,  #一次性打包32条给模型
        normalize_embeddings=True,  #向量归一化
        convert_to_numpy=True,  #直接输出numpy数组
        show_progress_bar=False,  #关掉进度条，Streamlit 页面不会乱打印日志
    )
    #sentence-transformers 的 encode，默认出来的向量是float32或者float64的numpy数组。
    return np.asarray(vecs,dtype=np.float32)

def embed_query(text:str)->np.ndarray:
    return embed_texts([QUERY_INSTRUCTION+text])[0]


def cosine_similarity(a:np.ndarray,b:np.ndarray)->float:
    return float(np.dot(a,b))

class SimpleVectorStore:    
    def __init__(self,dim:int=EMBED_DIM):
        self.dim=dim  #向量维度
        self.vectors:list[np.ndarray]=[]   #向量仓库
        self.documents:list[str]=[]      #保存原始文本
        self.metadata:list[dict]=[]      #元数据
        self.ids:list[str]=[]           #文档唯一id
    #@observe()
    def add(self,texts:list[str],metadatas:list[dict]=None,ids:list[str]=None)->list[str]:   #=none:可以不传
        """
        批量添加文档
        """
        
        if isinstance(texts,str):  #判断变量类型，是否为列表，不是转为列表
            texts=[texts]
        texts=list(texts)
        if metadatas is None:
            metadatas=[{} for _ in texts]    #没有传元数据，按文本条数建多个独立空字典
        if ids is None:
            ids=[f"doc-{len(self.ids)+i:04d}"for i in range(len(texts))]
        if not (len(texts)==len(metadatas)==len(ids)):
            raise ValueError(
                f"texts/metadatas/ids 长度不一致：{len(texts)}/{len(metadatas)}/{len(ids)}"
            )
        vecs=embed_texts(texts)
        for vec,text,meta,doc_id in zip (vecs,texts,metadatas,ids):
            self.vectors.append(vec)    #pseudo_embed()生成伪向量，追加到vectors列表
            self.documents.append(text)
            self.metadata.append(meta)
            self.ids.append(doc_id)
        return ids
    @observe(
            name="bi_retrieve",
            as_type="retriever",
            capture_input=False,
            capture_output=False
            )
    def query(self,query_text:str,top_k:int=5,metadata_filter:dict=None)->list[dict]:    #metadata_filter元数据过滤条件
        """
        语义检索：返回TopK最相似的文档
        """
        get_client().update_current_span(
            input={"query": query_text, "top_k": top_k}
        )
        if not self.vectors:
            return []
        q_vec=embed_query(query_text)
        scores=[cosine_similarity(q_vec,v)for v in self.vectors]

        ranked=sorted(
            enumerate(scores),   #enumerate同时拿到文档下标和相似度
            key=lambda x:x[1],    #指定用相似度作为排序字段
            reverse=True   #降序排序
        )

        results=[]
        for idx,score in ranked:
            if metadata_filter:     #判断有无筛选条件
                if not all(self.metadata[idx].get(k)==v
                for k,v in metadata_filter.items()):
                    continue
            results.append({
                "id":self.ids[idx],
                "document":self.documents[idx],
                "metadata": self.metadata[idx],
                "similarity":score,
            })
            if len(results)>=top_k:
                break

        get_client().update_current_span(
            output=[{"id": r["id"], "score": round(r["similarity"], 4)}
                    for r in results]
        )        
        return results


def cross_encoder_score(query:str,document:str)->float:
    """关键词匹配+滑动窗口语义打分(模拟rerank)"""
    score=0.0
    q_lower=query.lower()
    d_lower=document.lower()

    for seg_len in [4,3,2]: 
        for i in range(len(q_lower)-seg_len+1):  
            seg=q_lower[i:i+seg_len]   #从问句的 i 位置，截取一段长度为 seg_len 的连续文本
            if seg in d_lower:  #判断：这个从问句切出来的短语，有没有出现在文档里面
                score+=seg_len*0.015

    q_words=set(q_lower)  #把字符串转成字符集合，每个字符保留一份，方便后续遍历(去重)
    d_words=list(d_lower)  
    for w in q_words:
        tf=d_words.count(w)  #词频，统计列表里面w出现次数
        if tf >0:
            score+=min(tf,5)*0.03
    return min(score,1.0)

@observe(name="two_stage_retrieve", as_type="retriever",
         capture_input=False, capture_output=False)
def two_stage_retrieve(query:str,store:SimpleVectorStore,recall_k:int=10,final_k:int=3)->list[dict]:
    """两阶段检索: Bi-Encoder 召回 + Cross-Encoder 精排"""
    get_client().update_current_span(
        input={"query": query, "recall_k": recall_k, "final_k": final_k}
    )
    #stage1:Bi粗召回10条
    candidates=store.query(query,top_k=recall_k)
    #stage2:模拟Cross-Encoder打分重排
    for c in candidates:
        c['rerank_score']=cross_encoder_score(query,c['document'])

    #按rerank分数降序
    candidates.sort(key=lambda x:x['rerank_score'],reverse=True)  # lambda 参数 : 返回值 sort列表原地排序方法，reverse = 反转；用每条rerank_score这个值来比大小
    final_res=candidates[:min(final_k,recall_k)]
    for idx,item in enumerate(final_res,start=1):
        item["ref_id"]=f"[{idx}]"

    get_client().update_current_span(
        output=[{"id": c["id"], "rerank": c["rerank_score"]}
                for c in final_res],
        metadata={"recall_count": len(candidates)},
    )
    return final_res

if __name__=="__main__":
    chunk_list,stats=load_kb(
        root_dir="kb_documents",
        chunk_size=256,
        overlap=26,
        split_strategy="sentence",
    )
    store = SimpleVectorStore(dim=512)
    store.add(
        texts=[c["text"]for c in chunk_list],
        metadatas=[c["meta"]for c in chunk_list],
    )
    print(f"入库完成：{len(store.ids)}条")
    query="为什么 InnoDB 选择 B+ 树，而不是 B 树或哈希索引？	"
    res=two_stage_retrieve(query, store, recall_k=60, final_k=3)
    print("\n=======两阶段检索结果（Bi + Rerank）Top3=======")
    for item in res:
        ref_tag=item["ref_id"]
        print(f"\n{ref_tag}")
        print(f"doc_id:{item['id']}")
        print(f"领域:{item['metadata']['domain']}|chunk_id:{item['metadata']['chunk_id']} | 文件: {item['metadata']['source_file']}")
        print(f"Bi相似度(similarity):{item['similarity']:.4f}")
        print(f"Rerank打分(rerank_score):{item['rerank_score']:.4f}")
        print(f"chunk原文：\n{item['document']}")
        
