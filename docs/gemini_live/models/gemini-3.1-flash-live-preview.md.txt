# Gemini 3.1 Flash Live Preview

<br />

Gemini 3.1 Flash Live Preview is our low-latency, audio-to-audio model optimized
for real-time dialogue and voice-first AI applications with acoustic nuance
detection, numeric precision, and multimodal awareness.
[Try in Google AI Studio](https://aistudio.google.com/live?model=gemini-3.1-flash-live-preview)

## Documentation

Visit the [Live API](https://ai.google.dev/gemini-api/docs/live-api) guide for full coverage
of features and capabilities.

## gemini-3.1-flash-live-preview

| Property | Description |
|---|---|
| Model code | `gemini-3.1-flash-live-preview` |
| Supported data types | **Inputs** Text, images, audio, video **Output** Text and audio |
| Token limits^[\[\*\]](https://ai.google.dev/gemini-api/docs/tokens)^ | **Input token limit** 131,072 **Output token limit** 65,536 |
| Capabilities | **Audio generation** Supported **Batch API** Not supported **Caching** Not supported **Code execution** Not supported **File search** Not Supported **Function calling** Supported **Grounding with Google Maps** Not supported **Image generation** Not supported **Live API** Supported **Search grounding** Supported **Structured outputs** Not supported **Thinking** Supported **URL context** Not supported |
| Versions | Read the [model version patterns](https://ai.google.dev/gemini-api/docs/models/gemini#model-versions) for more details. - Preview: `gemini-3.1-flash-live-preview` |
| Latest update | March 2026 |
| Knowledge cutoff | January 2025 |

## Migrating from Gemini 2.5 Flash Live

Gemini 3.1 Flash Live Preview is optimized for low-latency, real-time dialogue.
When migrating from `gemini-2.5-flash-native-audio-preview-12-2025`, consider
the following:

- **Model string** : Update your model string from `gemini-2.5-flash-native-audio-preview-12-2025` to `gemini-3.1-flash-live-preview`.
- **Thinking configuration** : Gemini 3.1 uses `thinkingLevel` (with settings like `minimal`, `low`, `medium`, and `high`) instead of `thinkingBudget`. The default is `minimal` to optimize for lowest latency. See [Thinking levels and budgets](https://ai.google.dev/gemini-api/docs/thinking#levels-budgets).
- **Server events** : A single [`BidiGenerateContentServerContent`](https://ai.google.dev/api/live#bidigeneratecontentservercontent) event can now contain multiple content parts simultaneously (for example, audio chunks and transcript). Update your code to process all parts in each event to avoid missing content.
- **Client content** : `send_client_content` is only supported for seeding initial context history (requires setting [`initial_history_in_client_content`](https://ai.google.dev/api/live#HistoryConfig) in [`history_config`](https://ai.google.dev/api/live#BidiGenerateContentSetup)). Use [`send_realtime_input`](https://ai.google.dev/api/live#bidigeneratecontentrealtimeinput) to send text updates during the conversation. See [Incremental content updates](https://ai.google.dev/gemini-api/docs/live-guide#incremental-updates).
- **Turn coverage** : Defaults to [`TURN_INCLUDES_AUDIO_ACTIVITY_AND_ALL_VIDEO`](https://ai.google.dev/api/live#turncoverage) instead of `TURN_INCLUDES_ONLY_ACTIVITY`. The model's turn now includes detected audio activity and all video frames. If your application currently sends a constant stream of video frames, you may want to update your application to only send video frames when there is audio activity to avoid incurring additional costs.
- **Async function calling** : Not yet supported. Function calling is synchronous only. The model will not start responding until you've sent the tool response. See [Async function calling](https://ai.google.dev/gemini-api/docs/live-tools#async-function-calling).
- **Proactive audio and affective dialogue** : These features are not yet supported in Gemini 3.1 Flash Live. Remove any configuration for these features from your code. See [Proactive audio](https://ai.google.dev/gemini-api/docs/live-guide#proactive-audio) and [Affective dialogue](https://ai.google.dev/gemini-api/docs/live-guide#affective-dialog).

For a detailed feature comparison, see the
[Model comparison](https://ai.google.dev/gemini-api/docs/live-guide#model-comparison) table in the
capabilities guide.