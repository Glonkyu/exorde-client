# -*- coding: utf-8 -*-
"""
Created on Mon Apr 17 09:12:00 2023

@author: flore
"""

from huggingface_hub import hf_hub_download
import json
import numpy as np
import os
import pandas as pd
import re
import requests
from sentence_transformers import SentenceTransformer
import spacy
import swifter
import tensorflow as tf
import torch
from transformers import AutoTokenizer, pipeline
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    

### VARIABLE INSTANTIATION

device = torch.cuda.current_device() if torch.cuda.is_available() else -1
classifier = pipeline("zero-shot-classification", model="MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli", device=device, batch_size=16, top_k=None, max_length=64)
labels =  requests.get("https://raw.githubusercontent.com/exorde-labs/TestnetProtocol/main/targets/cateogry_tree.json").json()
mappings = {
    "Gender":{0:"Female", 1:"Male"},
    "Age":{0:"<20", 1:"20<30",2:"30<40",3:">=40"},
    "HateSpeech":{0:"Hate speech", 1:"Offensive", 2:"None"}
    }
try:
    nlp = spacy.load("en_core_web_trf")
except:
    os.system("python -m spacy download en_core_web_sm") # Download the model if not present
    nlp = spacy.load("en_core_web_trf")


### REQUIRED CLASSES (j'ai enlevé autant de classes que possible mais celles-ci sont nécessaires x)

class TransformerBlock(tf.keras.layers.Layer):
    def __init__(self, embed_dim, num_heads, ff_dim, rate=0.1, **kwargs):
        super().__init__()
        self.att = tf.keras.layers.MultiHeadAttention(num_heads=num_heads, key_dim=embed_dim)
        self.ffn = tf.keras.Sequential(
            [tf.keras.layers.Dense(ff_dim, activation="relu"), tf.keras.layers.Dense(embed_dim),]
        )
        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)

    def call(self, inputs, training):
        attn_output = self.att(inputs, inputs)
        attn_output = self.dropout1(attn_output, training=training)
        out1 = self.layernorm1(inputs + attn_output)
        ffn_output = self.ffn(out1)
        ffn_output = self.dropout2(ffn_output, training=training)
        return self.layernorm2(out1 + ffn_output)
    
class TokenAndPositionEmbedding(tf.keras.layers.Layer):
    def __init__(self, maxlen, vocab_size, embed_dim, **kwargs):
        super().__init__()
        self.token_emb = tf.keras.layers.Embedding(input_dim=vocab_size, output_dim=embed_dim)
        self.pos_emb = tf.keras.layers.Embedding(input_dim=maxlen, output_dim=embed_dim)

    def call(self, x):
        maxlen = tf.shape(x)[-1]
        positions = tf.range(start=0, limit=maxlen, delta=1)
        positions = self.pos_emb(positions)
        x = self.token_emb(x)
        return x + positions

### FUNCTIONS DEFINITION

# This method below should be called before the freshness test.
# Requires no empty string // no None text
# Order:
#     - Spotting
#     - Zero-shot classification
#     - Freshness test (does the item has been posted less thant 5 minutes ago ?)
def zero_shot(texts, labeldict, max_depth = None, depth = 0):   
    
    """
        Perform zero-shot classification on the input text using a pre-trained language model.
        
        Args:
        - text (str): The input text to be classified.
        - labeldict (dict): A dictionary that maps each label to its corresponding sub-labels, or None if the label has no sub-labels.
        - path (list, optional): A list containing the path of labels from the root to the current label. Defaults to None.
        - depth (int, optional): The current depth in the label hierarchy. Defaults to 0.
        - max_depth (int, optional): The maximum depth in the label hierarchy to explore. Defaults to None (i.e., explore the entire hierarchy).
        
        Returns:
        - path (list): A list containing the path of labels from the root to the predicted label. If the label hierarchy was not explored fully and the max_depth parameter was set, the path may not be complete.
    """
    
    keys = list(labeldict.keys())
    output=classifier(texts, keys, multi_label=False, max_length=32)
    labels = [output["labels"][0]]
    depth += 1
    if(depth == max_depth):
        _labels = labels
        return labels
    else:
        _labels = dict()
        for lab in labels:
            keys = list(labeldict[lab].keys())
            output=classifier(texts, keys, multi_label=False, max_length=32)
            _out = list()
            for i in range(len(output["labels"])):
                
                scores = (output["labels"][i], output["scores"][i])
                _out.append(scores)

            # _labs = [output[x]["labels"] for x in range(len(output))]
            # _scores = [output[x]["scores"] for x in range(len(output))]
            
            _labels[lab] = _out
    return _labels

def preprocess_text(text: str, remove_stopwords: bool) -> str:
    """This utility function sanitizes a string by:
    - removing links
    - removing special characters
    - removing numbers
    - removing stopwords
    - transforming in lowercase
    - removing excessive whitespaces
    Args:
        text (str): the input text you want to clean
        remove_stopwords (bool): whether or not to remove stopwords
    Returns:
        str: the cleaned text
    """
    def contains_only_special_chars(s):
        pattern = r'^[^\w\s]+$'
        return bool(re.match(pattern, s))
    
    def preprocess(text):
        new_text = [wrd for wrd in text.split(" ") if  wrd.startswith("@") == False and wrd.startswith("http") == False] 
        return " ".join(new_text)
    text = text.replace("#", "")
    text = preprocess(text)
    text = text.lower().strip()
    
    if(contains_only_special_chars(text)):
        text = ""
    return text

def get_entities(text):
    doc = nlp(text)
    return [(x.text, x.label_) for x in doc.ents]

def predict(text, pipe, tag):
    preds = pipe.predict(text, verbose=0)[0]
    result = []
    for i in range(len(preds)):
        result.append((mappings[tag][i], preds[i]))
    return result

def tag(documents):

    tmp = pd.DataFrame()
    
    tmp["Translation"] = documents
    
    model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
    tmp["Embeddings"] = tmp["Translation"].swifter.apply(lambda x: model.encode(x))
    #tmp["Embeddings"] = model.encode(tmp["Translation"])
    
    pipe = pipeline("text-classification", model="djsull/kobigbird-spam-multi-label", device=device, top_k=None)
    tmp["Advertising"] = tmp["Translation"].swifter.apply(lambda x: tuple(pipe(x)[0]))
    
    # if(len(keep) > 0):
    #     tmp = tmp[tmp["Advertising"].isin(keep)]
    
    tmp["Entities"] = tmp["Translation"].swifter.apply(lambda x: get_entities(x))
    
    pipe = pipeline("text-classification", model="j-hartmann/emotion-english-distilroberta-base", device=device, top_k=None)
    tmp["Emotion"] = pipe(list(tmp["Translation"]),batch_size=250, )
    
    pipe = pipeline("text-classification", model="cardiffnlp/twitter-roberta-base-irony", device=device, top_k=None)
    tmp["Irony"] = tmp["Translation"].swifter.apply(lambda x: tuple(pipe(x)[0]))
    
    pipe = pipeline("text-classification", model="salesken/query_wellformedness_score", device=device, top_k=None)
    tmp["LanguageScore"] = tmp["Translation"].swifter.apply(lambda x: round(pipe(x)[0][0]["score"], 2))
    
    pipe = pipeline("text-classification", model="marieke93/MiniLM-evidence-types", device=device, top_k=None)
    tmp["TextType"] = tmp["Translation"].swifter.apply(lambda x: tuple(pipe(x)[0]))
    
    pipe = pipeline("text-classification", model="alimazhar-110/website_classification", device=device, top_k=None)
    tmp["SourceType"] = tmp["Translation"].swifter.apply(lambda x: tuple(pipe(x)[0]))

    
    
    ### HOMEMADE MODELS
    tokenizer = AutoTokenizer.from_pretrained("bert-large-uncased")
    embedded = [np.array(tokenizer.encode_plus(x, add_special_tokens=True, max_length=512,truncation=True, pad_to_max_length=True, return_attention_mask=False, return_tensors='tf')["input_ids"][0]).reshape(1, -1) for x in list(tmp["Translation"])]
    tmp["Embedded"] = embedded
    
    tmp_emoji_lexicon = hf_hub_download(repo_id="ExordeLabs/SentimentDetection", filename="emoji_unic_lexicon.json")
    tmp_loughran_dict = hf_hub_download(repo_id="ExordeLabs/SentimentDetection", filename="loughran_dict.json")
    with open(tmp_emoji_lexicon) as f:
        unic_emoji_dict=json.load(f)
    with open(tmp_loughran_dict) as f:
        Loughran_dict=json.load(f)
    pipe = SentimentIntensityAnalyzer()
    pipe.lexicon.update(Loughran_dict)
    pipe.lexicon.update(unic_emoji_dict)
    tmp["Sentiment"] = tmp["Translation"].swifter.apply(lambda x: pipe.polarity_scores(x)['compound'])  
    
    tmp_fileName = hf_hub_download(repo_id="ExordeLabs/AgeDetection", filename="ageDetection.h5")
    pipe = tf.keras.models.load_model(tmp_fileName, custom_objects = {"TokenAndPositionEmbedding": TokenAndPositionEmbedding,"TransformerBlock": TransformerBlock})
    tmp["Age"] = tmp["Embedded"].swifter.apply(lambda x: predict(x, pipe, "Age"))
    
    tmp_fileName = hf_hub_download(repo_id="ExordeLabs/GenderDetection", filename="genderDetection.h5")
    pipe = tf.keras.models.load_model(tmp_fileName, custom_objects = {"TokenAndPositionEmbedding": TokenAndPositionEmbedding,"TransformerBlock": TransformerBlock})
    tmp["Gender"] = tmp["Embedded"].swifter.apply(lambda x: predict(x, pipe, "Gender"))
    
    tmp_fileName = hf_hub_download(repo_id="ExordeLabs/HateSpeechDetection", filename="hateSpeechDetection.h5")
    pipe = tf.keras.models.load_model(tmp_fileName, custom_objects = {"TokenAndPositionEmbedding": TokenAndPositionEmbedding,"TransformerBlock": TransformerBlock})

    return tmp


# ### TEST ZONE
# test = ["""Bitcoin hit 30.000$ last night! """, "I like having a mojito with my breakfast"]
# field = zero_shot(test, labels, max_depth=1) ==> ['Economy and Finance', 'Lifestyle and Traditions']
# print(field)


