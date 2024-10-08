from dataclasses import dataclass
from logging import getLogger
from typing import List, Optional, Dict
from langkit import lang_config
from nltk.tokenize import sent_tokenize
from langkit.openai.openai import  Conversation, ChatLog
from langkit.transformer import Encoder
from sentence_transformers import util
from langkit.openai import OpenAILegacy

diagnostic_logger = getLogger(__name__)

embeddings_encoder = Encoder(lang_config.transformer_name, custom_encoder=None)

@dataclass
class ConsistencyResult:
    """
    This class represents the result of a consistency check.

    llm_score: float
        The score given by the llm-based check. It is the average of the scores
        given by the llm between the response and each sample.
    semantic_score: float
        The score given by the semantic similarity-based check.
    final_score: float
        The average of the llm_score and the semantic_score.
    total_tokens: int
        The total number of tokens used by the llm to generate the samples and
        to check the consistency.
    samples: List[str]
        The list of samples generated by the llm.
    response: str
        The response to be checked.


    """

    llm_score: float
    semantic_score: float
    final_score: float
    samples: List[str]
    response: str
    prompt_response_pair: List[Dict]

    def to_summary_dict(self):
        return {
            "llm_score": self.llm_score,
            "semantic_score": self.semantic_score,
            "final_score": self.final_score,
            "samples": self.samples,
            "response": self.response,
            "prompt_response_pair": self.prompt_response_pair
        }


class ConsistencyChecker:
    """
    This class is responsible for checking the consistency of a response
    generated by a language model.

    It uses a sample generator language model to generate samples of responses
    to a prompt.The llm used to generate the samples should be the same as the one used
    to generate the response.


    The additional samples are used to check the consistence between samples and
    original response. The consistency check is done by combining an llm-based check
    with a semantic similarity-based check.

    This approach was inspired by: https://arxiv.org/abs/2303.08896
    """

    def __init__(self, rag_pipeline, num_samples, embeddings_encoder):
        self.num_samples = num_samples
        self.rag_pipeline = rag_pipeline
        consistency_checker_llm = OpenAILegacy(model="gpt-3.5-turbo-instruct")
        consistency_checker_llm.temperature = 0
        self.consistency_checker_llm = consistency_checker_llm
        self.embeddings_encoder = embeddings_encoder

    def convert_score(self, result):
        categories = {
            "major inaccurate": 1.0,
            "minor inaccurate": 0.5,
            "accurate": 0.0,
        }
        score = categories.get(result.lower().strip(), None)
        if score is None:
            diagnostic_logger.info(
                f"Invalid result from consistency checker: {result}. Valid results are {categories.keys()}"
            )
            score = 0.0
        return score

    def get_samples(self, prompt:str)-> List[str]:
        samples = []
        for _ in range(self.num_samples):
            response:str = self.rag_pipeline.invoke(prompt).get('answer')
            samples.append(response)
        return samples

    def sentence_semantic_score(
        self, response_sentence, samples_list, embeddings_encoder
    ):
        sample_similarities = []
        for sample in samples_list:
            sample_sentences = sent_tokenize(sample)
            sentence_similarities = [
                util.pytorch_cos_sim(
                    embeddings_encoder.encode(response_sentence),
                    embeddings_encoder.encode(sample_sentence),
                ).item()
                for sample_sentence in sample_sentences
            ]
            if sentence_similarities:
                sample_similarities.append(max(sentence_similarities))
        sentence_score = 1 - sum(sample_similarities) / len(sample_similarities)
        return sentence_score

    def semantic_score(self, response, additional_samples: List[str]):
        """
        This function calculates the semantic score between the response and
        the samples generated by the llm.

        The semantic score is calculated per sentence and then averaged.

        """
        response_sentences = sent_tokenize(response)
        samples_list = [sample for sample in additional_samples]
        response_similarities = [
            self.sentence_semantic_score(
                sentence, samples_list, self.embeddings_encoder
            )
            for sentence in response_sentences
        ]
        final_score = sum(response_similarities) / len(response_similarities)
        return final_score

    def llm_consistency_check(self, response, additional_samples):
        """
        This function calculates the llm score by asking the llm to perform a consistency check between the
        response and each sample.

        The llm score is calculated per half of the response and then averaged.

        """
        prompt_response_pair = []
        llm_scores = []
        response_sentences = sent_tokenize(response)
        response_halves = [
            "".join(response_sentences[:2]),
            "".join(response_sentences[2:]),
        ]
        response_halves = [half for half in response_halves if half]
        for response_half in response_halves:
            llm_scores_half = []
            for sample in additional_samples:
                prompt = f"""Context: {sample}

Passage: {response_half}

Is the passage supported by the context above?
Answer between: Accurate, Minor Inaccurate, Major Inaccurate

Don't include additional information/explanation. Please answer only with the options above.

Answer:
"""
                result: ChatLog = Conversation(
                    self.consistency_checker_llm
                ).send_prompt(prompt)
                llm_score = self.convert_score(result.response)
                llm_scores_half.append(llm_score)
                prompt_response_pair.append({
                                    "prompt": prompt,
                                    "response": result.response,
                                    "score": llm_score
                                             })
            llm_scores.append(sum(llm_scores_half) / len(llm_scores_half))
        final_score = sum(llm_scores) / len(llm_scores)
        return final_score, prompt_response_pair

    def consistency_check(
        self, prompt: str, response: Optional[str] = None
    ) -> ConsistencyResult:

        if not response:
            raise Exception(f"Please pass the response")

        additional_samples: List[str] = self.get_samples(prompt)

        llm_score, prompt_response_pair = self.llm_consistency_check(
            response, additional_samples
        )

        semantic_score = self.semantic_score(response, additional_samples)
        consistency_result = ConsistencyResult(
            llm_score=llm_score,
            semantic_score=semantic_score,
            final_score=(llm_score + semantic_score) / 2,
            samples=[sample for sample in additional_samples],
            response=response,
            prompt_response_pair=prompt_response_pair
        )
        return consistency_result
    
checker: Optional[ConsistencyChecker] = None


def init(rag_pipeline, num_samples=1):
    global checker
    import nltk

    nltk.download("punkt")
    diagnostic_logger.info(
        "Info: the response_hallucination metric module performs additionall LLM calls to check the consistency of the response."
    )
    checker = ConsistencyChecker(rag_pipeline, num_samples, embeddings_encoder)


def consistency_check(prompt: str, response: Optional[str] = None):
    if checker is not None:
        return checker.consistency_check(prompt, response).to_summary_dict()
    else:
        raise Exception("You need to call init() before using this function")
