#!/usr/bin/env python2

# check profiler
if not isinstance(__builtins__, dict) or "profile" not in __builtins__:
    __builtins__.__dict__["profile"] = lambda x: x

from misc import util
from misc.indices import QUESTION_INDEX, ANSWER_INDEX, MODULE_INDEX, \
        MODULE_TYPE_INDEX, NULL, NULL_ID, UNK_ID
from misc.visualizer import visualizer
import models
import tasks
from models.nmn import *

import apollocaffe
import argparse
import json
import logging.config
import random
import numpy as np
import yaml

def main():
    config = configure()
    model = models.build_model(config.model, config.opt)

    #config.task.subset = 100
    #tasks.load_task(config)

    #for i_subset in range(5):
    #for i_subset in [10]:
    #config.task.subset = i_subset
    task = tasks.load_task(config)

    for i_epoch in range(config.opt.iters):

        train_loss, train_acc, _ = \
                do_iter(task.train, model, config, train=True)
        val_loss, val_acc, val_predictions = \
                do_iter(task.val, model, config, vis=True)
        test_loss, test_acc, _ = \
                do_iter(task.test, model, config)

        logging.info(
                "%5d  |  %2.3f  %2.3f  %2.3f  |  %2.3f  %2.3f  %2.3f",
                i_epoch,
                train_loss, val_loss, test_loss,
                train_acc, val_acc, test_acc)

        if config.opt.log_preds:
            with open("logs/val_predictions_%d.json" % i_epoch, "w") as pred_f:
                print >>pred_f, json.dumps(val_predictions)

def configure():
    apollocaffe.set_random_seed(0)
    np.random.seed(0)
    random.seed(0)

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument(
            "-c", "--config", dest="config", required=True,
            help="model configuration file")
    arg_parser.add_argument(
            "-l", "--log-config", dest="log_config", default="config/log.yml",
            help="log configuration file")

    args = arg_parser.parse_args()
    config_name = args.config.split("/")[-1].split(".")[0]

    with open(args.log_config) as log_config_f:
        log_filename = "logs/%s.log" % config_name
        log_config = yaml.load(log_config_f)
        log_config["handlers"]["fileHandler"]["filename"] = log_filename
        logging.config.dictConfig(log_config)

    with open(args.config) as config_f:
        config = util.Struct(**yaml.load(config_f))

    assert not hasattr(config, "name")
    config.name = config_name

    return config

def do_iter(task_set, model, config, train=False, vis=False):
    loss = 0.0
    acc = 0.0
    predictions = []
    n_batches = 0

    # sort first to guarantee deterministic behavior with fixed seed
    data = list(sorted(task_set.data))
    np.random.shuffle(data)

    if vis:
        visualizer.begin(config.name, 100)

    for batch_start in range(0, len(data), config.opt.batch_size):
        batch_end = batch_start + config.opt.batch_size
        batch_data = data[batch_start:batch_end]

        batch_loss, batch_acc, batch_preds = do_batch(
                batch_data, model, config, train, vis)

        loss += len(batch_data) * batch_loss
        acc += len(batch_data) * batch_acc
        predictions += batch_preds
        n_batches += 1

        if vis:
            datum = batch_data[0]
            preds = model.prediction_data[0,:]
            top = np.argsort(preds)[-5:]
            top_answers = reversed([ANSWER_INDEX.get(p) for p in top])
            fields = [
                    " ".join([QUESTION_INDEX.get(w) for w in datum.question[1:-1]]),
                "<img src='../../%s'>" % datum.image_path,
                model.att_data[0,...],
                ", ".join(top_answers),
                ", ".join([ANSWER_INDEX.get(a) for a in datum.answers])
            ]
            visualizer.show(fields)

    if vis:
        visualizer.end()

    if n_batches == 0:
        return 0, 0, dict()
    assert len(predictions) == len(data)
    loss /= len(data)
    acc /= len(data)
    return loss, acc, predictions

def do_batch(data, model, config, train, vis):
    #print "\n"
    #print "\n".join([str(d.layouts[0].labels) for d in data])
    predictions = forward(data, model, config, train, vis)
    answer_loss = backward(data, model, config, train, vis)
    if not train:
        acc = compute_acc(predictions, data, config)
    else:
        acc = compute_acc(predictions, data, config)
        #acc = 0

    return answer_loss, acc, predictions

def forward(data, model, config, train, vis):
    model.reset()

    # load batch data
    max_len = max(len(d.question) for d in data)
    max_layouts = max(len(d.layouts) for d in data)
    channels, image_size, trailing = data[0].load_features().shape
    assert trailing == 1
    rel_channels, is1, is2 = data[0].load_rel_features().shape
    assert is1 == is2 == image_size
    questions = np.ones((config.opt.batch_size, max_len)) * NULL_ID
    features = np.zeros((config.opt.batch_size, channels, image_size, 1))
    rel_features = np.zeros((config.opt.batch_size, rel_channels, image_size, image_size))
    layout_reprs = np.zeros((config.opt.batch_size, max_layouts, len(MODULE_INDEX) + 10))

    for i, datum in enumerate(data):
        questions[i, max_len-len(datum.question):] = datum.question
        features[i, ...] = datum.load_features()
        rel_features[i, ...] = datum.load_rel_features()
        for i_layout in range(len(datum.layouts)):
            layout = datum.layouts[i_layout]
            labels = util.flatten(layout.labels)
            modules = util.flatten(layout.modules)
            for i_mod in range(len(modules)):
                if isinstance(modules[i_mod], AttendModule):
                    layout_reprs[i, i_layout, labels[i_mod]] += 1

                mt = MODULE_TYPE_INDEX.index(modules[i_mod])
                layout_reprs[i, i_layout, len(MODULE_INDEX) + mt] += 1

            #print layout
            #exit()
            #feats = util.flatten(datum.layouts[i_layout].labels)
            #print feats
            #layout_reprs[i,i_layout,feats] = 1
    layouts = [d.layouts for d in data]

    # apply model
    model.forward(
            layouts, layout_reprs, questions, features, rel_features,
            dropout=(train and config.opt.dropout),
            deterministic=not train)

    # extract predictions
    predictions = list()
    if config.opt.multiclass:
        pred_words = []
        for i in range(model.prediction_data.shape[0]):
            preds = model.prediction_data[i, :]
            chosen = np.where(preds > 0.5)[0]
            pred_words.append(set(ANSWER_INDEX.get(w) for w in chosen))
    else:
        pred_ids = np.argmax(model.prediction_data, axis=1)
        pred_words = [ANSWER_INDEX.get(w) for w in pred_ids]

    for i in range(len(data)):
        qid = data[i].id
        answer = pred_words[i]
        predictions.append({"question_id": qid, "answer": answer})

    return predictions

def backward(data, model, config, train, vis):
    n_answers = len(data[0].answers)
    loss = 0

    for i in range(n_answers):
        if config.opt.multiclass:
            output_i = np.zeros((config.opt.batch_size, len(ANSWER_INDEX)))
            for i_datum, datum in enumerate(data):
                for answer in datum.answers[i]:
                    output_i[i_datum, answer] = 1
        else:
            output_i = UNK_ID * np.ones(config.opt.batch_size)
            output_i[:len(data)] = \
                    np.asarray([d.answers[i] for d in data])

        loss += model.loss(output_i, multiclass=config.opt.multiclass)

    if train:
        model.train()

    return loss

def compute_acc(predictions, data, config):
    score = 0.0
    for prediction, datum in zip(predictions, data):
        pred_answer = prediction["answer"]
        if config.opt.multiclass:
            answers = [set(ANSWER_INDEX.get(aa) for aa in a) for a in datum.answers]
        else:
            answers = [ANSWER_INDEX.get(a) for a in datum.answers]

        #print " ".join([QUESTION_INDEX.get(w) for w in datum.question])
        #print pred_answer
        #print answers
        #print

        matching_answers = [a for a in answers if a == pred_answer]
        if len(answers) == 1:
            score += len(matching_answers)
        else:
            score += min(len(matching_answers) / 3.0, 1.0)
    score /= len(data)
    return score

def do_old_iter(data, model, config, train=False, vis=False):
    loss = 0.0
    acc = 0.0
    batches = 0
    layout_types = list(data.by_layout_type.keys())
    np.random.shuffle(layout_types)

    predictions = dict()

    if vis:
        visualizer.begin(config.name, 100)

    for layout_type in layout_types:
        key_data = list(data.by_layout_type[layout_type])
        np.random.shuffle(key_data)
        for batch_start in range(0, len(key_data), config.opt.batch_size):
            batch_end = batch_start + config.opt.batch_size
            batch_data = key_data[batch_start:batch_end]
            batches += 1

            channels, width, height = batch_data[0].load_image().shape
            length = max(len(d.question) for d in batch_data)
            n_answers = len(batch_data[0].answers)
            batch_images = np.zeros(
                    (config.opt.batch_size, channels, width, height))
            batch_questions = np.ones(
                    (config.opt.batch_size, length)) * QUESTION_INDEX[NULL]
            for i, datum in enumerate(batch_data):
                image = datum.load_image()
                d_length = len(datum.question)
                batch_images[i,...] = image
                batch_questions[i,length-d_length:] = datum.question

            batch_layout_labels = [d.layout.labels for d in batch_data]
            batch_layout_labels = util.tree_zip(*batch_layout_labels)
            model.forward(
                    layout_type, batch_layout_labels, batch_questions,
                    batch_images, dropout=train)
            for i in range(n_answers):
                batch_output_i = UNK_ID * np.ones(config.opt.batch_size)
                batch_output_i[:len(batch_data)] = \
                        np.asarray([d.answers[i] for d in batch_data])
                loss += model.loss(batch_output_i)[0]
                #acc += compute_acc(model.prediction_data, batch_output_i)
                acc += 0

            pred_ids = np.argmax(model.prediction_data, axis=1)
            pred_words = [ANSWER_INDEX.get(w) for w in pred_ids]
            for i in range(len(batch_data)):
                qid = batch_data[i].id
                answer = pred_words[i]
                predictions[qid] = {"question_id": qid, "answer": answer}

            if train:
                model.train()

            if vis:
                datum = batch_data[0]
                preds = model.prediction_data[0,:]
                top = np.argsort(preds)[-5:]
                top_answers = reversed([ANSWER_INDEX.get(p) for p in top])
                fields = [
                        " ".join([QUESTION_INDEX.get(w) for w in datum.question[1:-1]]),
                    "<img src='../../%s'>" % datum.image_path,
                    model.att_data[0,...],
                    ", ".join(top_answers),
                    ", ".join([ANSWER_INDEX.get(a) for a in datum.answers])
                ]
                visualizer.show(fields)

    if vis:
        visualizer.end()

    if batches == 0:
        return 0, 0, dict()
    return loss / batches, acc / batches, predictions

if __name__ == "__main__":
    main()
