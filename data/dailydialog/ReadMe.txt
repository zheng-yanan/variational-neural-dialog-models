Here are some explanations about the files:

1) dialogues_text.txt: The DailyDialog dataset which contains 11,318 transcribed dialogues.
2) dialogues_topic.txt: Each line in dialogues_topic.txt corresponds to the topic of that in dialogues_text.txt.
                        The topic number represents: {1: Ordinary Life, 2: School Life, 3: Culture & Education,
                        4: Attitude & Emotion, 5: Relationship, 6: Tourism , 7: Health, 8: Work, 9: Politics, 10: Finance}
3) dialogues_act.txt: Each line in dialogues_act.txt corresponds to the dialog act annotations in dialogues_text.txt.
                      The dialog act number represents: { 1: inform，2: question, 3: directive, 4: commissive }
4) dialogues_emotion.txt: Each line in dialogues_emotion.txt corresponds to the emotion annotations in dialogues_text.txt.
                          The emotion number represents: { 0: no emotion, 1: anger, 2: disgust, 3: fear, 4: happiness, 5: sadness, 6: surprise}

6) process.py:

	This file propcesses DailyDialog dataset.

	dailydialog.pkl constains a list dialog object.
	Each dialog object is a dictionary with keys {"topic", "utts"}.
	Each topic is an integer.
	Each utts is a list of utterance object.
	Each utterance object is a dictionary with keys {"floor", "text", "emot", "act"}.

	dailydialog_split.pkl is dictionary with keys {"train", "valid", "test"}.
	Each is a list of dialog objects, as above.
