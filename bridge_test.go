package snapchat

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/event"
	"maunium.net/go/mautrix/id"
)

type matrixMediaDownloadStub struct {
	bridgev2.MatrixAPI
	data []byte
}

func (stub matrixMediaDownloadStub) DownloadMedia(
	_ context.Context,
	_ id.ContentURIString,
	_ *event.EncryptedFileInfo,
) ([]byte, error) {
	return stub.data, nil
}

func TestStableMessageKeyIncludesChatAuthorBodyAndTimestamp(t *testing.T) {
	timestamp := time.Date(2026, 10, 23, 1, 2, 3, 0, time.UTC)
	base := stableMessageKey("chat1", "Anna", "hello", timestamp)
	tests := []struct {
		name      string
		chatID    string
		author    string
		body      string
		timestamp time.Time
	}{
		{name: "chat", chatID: "chat2", author: "Anna", body: "hello", timestamp: timestamp},
		{name: "author", chatID: "chat1", author: "David", body: "hello", timestamp: timestamp},
		{name: "body", chatID: "chat1", author: "Anna", body: "different", timestamp: timestamp},
		{name: "timestamp", chatID: "chat1", author: "Anna", body: "hello", timestamp: timestamp.Add(time.Second)},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := stableMessageKey(test.chatID, test.author, test.body, test.timestamp); got == base {
				t.Fatal("distinct message fields produced the same key")
			}
		})
	}
}

func TestRemoteTypingSenderUsesExistingRemoteChatParticipant(t *testing.T) {
	chat := localChat{ID: "chat1", Title: "Group"}
	messages := []localMessage{
		{Author: "David", IsCustom: true},
		{Author: "You"},
		{Author: "Anna"},
	}

	got := remoteTypingSender(chat, messages, networkid.UserLoginID("local-login"))

	if got.Sender != makeUserID("Anna") {
		t.Fatalf("typing sender = %q, want %q", got.Sender, makeUserID("Anna"))
	}
	if got.IsFromMe {
		t.Fatal("remote typing sender was marked as the local user")
	}
}

func TestDutchSelfAuthorMapsToConnectedBeeperUser(t *testing.T) {
	sender := messageEventSender("Ik", "local-login")
	if !sender.IsFromMe || sender.SenderLogin != "local-login" ||
		sender.Sender != localUserID {
		t.Fatalf("sender = %#v, want connected Beeper user", sender)
	}
}

func TestLocalStatusReadsTypingChatIDs(t *testing.T) {
	var state localStatus
	if err := json.Unmarshal(
		[]byte(`{"typing_chats":["chat1","chat2"],"opened_chats":["chat3"]}`),
		&state,
	); err != nil {
		t.Fatal(err)
	}
	if len(state.TypingChats) != 2 || state.TypingChats[0] != "chat1" ||
		state.TypingChats[1] != "chat2" {
		t.Fatalf("typing chats = %#v", state.TypingChats)
	}
	if len(state.OpenedChats) != 1 || state.OpenedChats[0] != "chat3" {
		t.Fatalf("opened chats = %#v", state.OpenedChats)
	}
}

func TestLatestChatMessageForOpenedReceipt(t *testing.T) {
	messages := []localMessage{
		{
			Author:    "You",
			Body:      "older sent message",
			Timestamp: "2026-10-27T09:00:00Z",
		},
		{
			Author:    "Alex",
			Body:      "reply",
			Timestamp: "2026-10-27T09:01:00Z",
		},
		{
			Author:    "You",
			Body:      "sent message",
			Timestamp: "2026-10-27T09:02:00Z",
		},
		{
			Author:    "Alex",
			Body:      "latest received message",
			Timestamp: "2026-10-27T09:03:00Z",
		},
	}

	message, timestamp, found, err := latestChatMessage("Alex", messages)
	if err != nil {
		t.Fatal(err)
	}
	if !found || message.Body != "latest received message" {
		t.Fatalf("latest chat message = %#v, found %t", message, found)
	}
	if got := timestamp.Format(time.RFC3339); got != "2026-10-27T09:03:00Z" {
		t.Fatalf("target timestamp = %q", got)
	}
}

func TestCapabilitiesAdvertiseOnlySupportedOutgoingImageTypes(t *testing.T) {
	client := &Client{}
	capabilities := client.GetCapabilities(t.Context(), nil)
	imageFeatures := capabilities.File[event.MsgImage]
	if imageFeatures == nil {
		t.Fatal("image file capabilities are missing")
	}
	if imageFeatures.MaxSize != maxOutgoingImageSize {
		t.Errorf("image max size = %d, want %d", imageFeatures.MaxSize, maxOutgoingImageSize)
	}
	for _, mimeType := range []string{"image/png", "image/jpeg", "image/gif"} {
		if got := imageFeatures.GetMimeSupport(mimeType); got != event.CapLevelFullySupported {
			t.Errorf("MIME support for %s = %v, want fully supported", mimeType, got)
		}
	}
	if capabilities.File[event.MsgAudio] != nil {
		t.Error("audio file capabilities are advertised for sending")
	}
	if got := imageFeatures.GetMimeSupport("video/mp4"); got != event.CapLevelRejected {
		t.Errorf("video MIME support = %v, want rejected", got)
	}
}

func TestHandleMatrixAudioRejectsSendingToSnapchat(t *testing.T) {
	client := &Client{}
	_, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
			Content: &event.MessageEventContent{
				MsgType: event.MsgAudio,
				Body:    "voice-note.wav",
			},
		},
	})
	if err == nil || !strings.Contains(err.Error(), "does not support sending audio") {
		t.Fatalf("HandleMatrixMessage() error = %v, want audio-send rejection", err)
	}
}

func TestValidateOutgoingImage(t *testing.T) {
	for _, test := range []struct {
		name string
		data []byte
		mime string
		want string
	}{
		{name: "PNG", data: []byte("\x89PNG\r\n\x1a\nimage"), mime: "image/png", want: "image/png"},
		{name: "JPEG", data: []byte("\xff\xd8\xffimage"), mime: "image/jpeg", want: "image/jpeg"},
		{name: "GIF", data: []byte("GIF89aimage"), mime: "image/gif", want: "image/gif"},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := validateOutgoingImage(test.data, &event.FileInfo{MimeType: test.mime})
			if err != nil {
				t.Fatalf("validateOutgoingImage() error = %v", err)
			}
			if got != test.want {
				t.Errorf("validateOutgoingImage() = %q, want %q", got, test.want)
			}
		})
	}
	if _, err := validateOutgoingImage([]byte("GIF89aimage"), &event.FileInfo{MimeType: "image/png"}); err == nil {
		t.Fatal("validateOutgoingImage() accepted a MIME type that does not match the file data")
	}
}

func TestHandleMatrixImageUploadsSupportedImageToLocalAPI(t *testing.T) {
	imageData := []byte("\x89PNG\r\n\x1a\nimage bytes")
	var requestBody []byte
	var requestType, requestChat, requestFilename string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/send-image" {
			t.Errorf("request path = %q, want /api/send-image", r.URL.Path)
		}
		requestChat = r.URL.Query().Get("chat_id")
		requestFilename = r.URL.Query().Get("filename")
		requestType = r.Header.Get("Content-Type")
		requestBody, _ = io.ReadAll(r.Body)
		_, _ = w.Write([]byte(`{"status":"sent"}`))
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{
			Config: &Config{LocalAPI: server.URL},
			Bridge: &bridgev2.Bridge{Bot: matrixMediaDownloadStub{data: imageData}},
		},
		http:    server.Client(),
		pending: make(map[string][]pendingEcho),
	}
	message, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
			Content: &event.MessageEventContent{
				MsgType: event.MsgImage,
				Body:    "image.png",
				URL:     id.ContentURIString("mxc://example.org/image"),
				Info:    &event.FileInfo{MimeType: "image/png"},
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
			InputTransactionID: "image-upload-1",
		},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessage() error = %v", err)
	}
	if message == nil || message.DB == nil {
		t.Fatal("HandleMatrixMessage() returned no message response")
	}
	if requestChat != "chat1" || requestFilename != "image.png" || requestType != "image/png" {
		t.Errorf(
			"image request metadata = chat %q, filename %q, MIME %q",
			requestChat,
			requestFilename,
			requestType,
		)
	}
	if !bytes.Equal(requestBody, imageData) {
		t.Errorf("image request body = %q, want %q", requestBody, imageData)
	}
}

func TestMediaMessageTypes(t *testing.T) {
	for _, test := range []struct {
		mediaType string
		eventType event.Type
		msgType   event.MessageType
	}{
		{mediaType: "image", eventType: event.EventMessage, msgType: event.MsgImage},
		{mediaType: "video", eventType: event.EventMessage, msgType: event.MsgVideo},
		{mediaType: "audio", eventType: event.EventMessage, msgType: event.MsgAudio},
		{mediaType: "sticker", eventType: event.EventSticker, msgType: ""},
	} {
		gotEventType, gotMsgType := mediaMessageTypes(test.mediaType)
		if gotEventType != test.eventType || gotMsgType != test.msgType {
			t.Errorf(
				"mediaMessageTypes(%q) = (%q, %q), want (%q, %q)",
				test.mediaType, gotEventType, gotMsgType, test.eventType, test.msgType,
			)
		}
	}
}

func TestSplitConvertedMessagePartsCapsBeeperMediaAtTen(t *testing.T) {
	parts := []*bridgev2.ConvertedMessagePart{{ID: "main"}}
	for index := 0; index < 23; index++ {
		parts = append(parts, &bridgev2.ConvertedMessagePart{
			ID: networkid.PartID("media-" + string(rune('a'+index))),
		})
	}

	batches := splitConvertedMessageParts(parts, maxBeeperMediaParts)
	wantSizes := []int{11, 10, 3}
	if len(batches) != len(wantSizes) {
		t.Fatalf("batch count = %d, want %d", len(batches), len(wantSizes))
	}
	partIndex := 0
	for batchIndex, batch := range batches {
		if len(batch) != wantSizes[batchIndex] {
			t.Errorf("batch %d has %d parts, want %d", batchIndex, len(batch), wantSizes[batchIndex])
		}
		mediaCount := 0
		for _, part := range batch {
			if part != parts[partIndex] {
				t.Errorf("part %d moved or changed while splitting", partIndex)
			}
			if strings.HasPrefix(string(part.ID), "media-") {
				mediaCount++
			}
			partIndex++
		}
		if mediaCount > maxBeeperMediaParts {
			t.Errorf("batch %d has %d media parts, limit is %d", batchIndex, mediaCount, maxBeeperMediaParts)
		}
	}
	if partIndex != len(parts) {
		t.Errorf("split retained %d of %d parts", partIndex, len(parts))
	}
}

func TestOutgoingImageQueueRejectsEleventhPendingImage(t *testing.T) {
	client := &Client{}

	for index := 0; index < maxQueuedOutgoingImages; index++ {
		messageID := fmt.Sprintf("image-%d", index)
		if !client.reserveOutgoingImage("chat1", messageID) {
			t.Fatalf("image %d unexpectedly rejected", index+1)
		}
	}
	if client.reserveOutgoingImage("chat1", "image-11") {
		t.Fatal("eleventh image in the burst was accepted")
	}
	if !client.reserveOutgoingImage("chat2", "other-chat-image") {
		t.Fatal("image in another chat was rejected")
	}
	client.releaseOutgoingImage("chat1", "image-0")
	if !client.reserveOutgoingImage("chat1", "image-11") {
		t.Fatal("new image was rejected after a queue slot was freed")
	}
}

func TestOutgoingImageSendsAreSerialized(t *testing.T) {
	client := &Client{}
	unlockFirst := client.lockOutgoingImage()
	secondStarted := make(chan struct{})
	secondDone := make(chan struct{})
	go func() {
		defer close(secondDone)
		unlockSecond := client.lockOutgoingImage()
		close(secondStarted)
		unlockSecond()
	}()

	select {
	case <-secondStarted:
		t.Fatal("second image send started before the first finished")
	case <-time.After(20 * time.Millisecond):
	}

	unlockFirst()
	select {
	case <-secondStarted:
	case <-time.After(time.Second):
		t.Fatal("second image send did not start after the first finished")
	}
	select {
	case <-secondDone:
	case <-time.After(time.Second):
		t.Fatal("second image send did not finish")
	}
}

func TestStickerEventContentUsesStickerSchema(t *testing.T) {
	eventType, msgType := mediaMessageTypes("sticker")
	if eventType != event.EventSticker || msgType != "" {
		t.Fatalf("sticker event types = (%q, %q), want (%q, empty)", eventType, msgType, event.EventSticker)
	}

	encoded, err := json.Marshal(&event.MessageEventContent{
		MsgType: msgType,
		Body:    "sticker.webp",
		URL:     "mxc://example.org/sticker",
		Info: &event.FileInfo{
			MimeType: "image/webp",
			Size:     12,
		},
	})
	if err != nil {
		t.Fatalf("marshal sticker content: %v", err)
	}
	var content map[string]any
	if err := json.Unmarshal(encoded, &content); err != nil {
		t.Fatalf("unmarshal sticker content: %v", err)
	}
	if _, hasMessageType := content["msgtype"]; hasMessageType {
		t.Fatalf("sticker content contains msgtype: %s", encoded)
	}
	for _, key := range []string{"body", "url", "info"} {
		if _, ok := content[key]; !ok {
			t.Errorf("sticker content is missing %q: %s", key, encoded)
		}
	}
}

func TestMakeUserIDRoundTripsNames(t *testing.T) {
	for _, name := range []string{"Anna daniels", "David Burshire", "名"} {
		id := makeUserID(name)
		encoded := string(id)[len("user:"):]
		decoded, err := decodeUserName(encoded)
		if err != nil {
			t.Fatalf("decodeUserName(%q): %v", name, err)
		}
		if decoded != name {
			t.Fatalf("decodeUserName() = %q, want %q", decoded, name)
		}
	}
}

func TestMessageEventSenderIdentifiesSelfForDatabasePersistence(t *testing.T) {
	const loginID = networkid.UserLoginID("login1")

	sender := messageEventSender(" You ", loginID)
	if !sender.IsFromMe {
		t.Fatal("self-authored message sender is not marked as from me")
	}
	if sender.SenderLogin != loginID {
		t.Errorf("SenderLogin = %q, want %q", sender.SenderLogin, loginID)
	}
	if sender.Sender != localUserID {
		t.Errorf("Sender = %q, want %q", sender.Sender, localUserID)
	}

	other := messageEventSender("John", loginID)
	if other.IsFromMe || other.SenderLogin != "" {
		t.Errorf("other sender = %#v, want a remote sender without a login identity", other)
	}
	if want := makeUserID("John"); other.Sender != want {
		t.Errorf("remote Sender = %q, want %q", other.Sender, want)
	}
}

func TestCanonicalReactionAuthorResolvesSelfAndChatParticipant(t *testing.T) {
	history := []localMessage{
		{Author: "You"},
		{Author: "  John   Smith "},
	}

	for _, test := range []struct {
		input string
		want  string
	}{
		{input: "You", want: "You"},
		{input: "you", want: "You"},
		{input: "John smith", want: "John Smith"},
		{input: "David Burshire", want: "You"},
	} {
		if got := canonicalReactionAuthor(test.input, history); got != test.want {
			t.Errorf("canonicalReactionAuthor(%q) = %q, want %q", test.input, got, test.want)
		}
	}
}

func TestCanonicalReactionAuthorDoesNotGuessInGroupChats(t *testing.T) {
	history := []localMessage{
		{Author: "John"},
		{Author: "Sam"},
	}

	if got := canonicalReactionAuthor("David Burshire", history); got != "David Burshire" {
		t.Fatalf("canonicalReactionAuthor() = %q, want unmatched group reactor", got)
	}
}

func TestCanonicalReactionAuthorMapsUnmatchedDirectChatAuthorToSelf(t *testing.T) {
	history := []localMessage{
		{Author: "You"},
		{Author: "John"},
	}
	if got := canonicalReactionAuthor("David Burshire", history); got != "You" {
		t.Fatalf("canonicalReactionAuthor() = %q, want self identity", got)
	}
	sender := messageEventSender(canonicalReactionAuthor("David Burshire", history), "login1")
	if !sender.IsFromMe || sender.SenderLogin != "login1" || sender.Sender != localUserID {
		t.Fatalf("self reaction sender = %#v, want connected Beeper identity", sender)
	}
}

func TestCustomMessageIdentityIsStableAndCannotBeUsedAsSnapchatReplyTarget(t *testing.T) {
	timestamp, err := parseTimestamp("2026-10-24T10:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	message := localMessage{
		Author:    "You",
		Body:      "Your Snapstreak is at risk!! ⌛️",
		Timestamp: "2026-10-24T10:00:00Z",
		IsCustom:  true,
		CustomID:  "custom-42",
	}

	if got := localMessageID("chat1", message, timestamp); got != "custom-42" {
		t.Fatalf("localMessageID() = %q, want persisted custom ID", got)
	}
	if target := findLocalReplyTarget("chat1", "custom-42", []localMessage{message}); target != nil {
		t.Fatalf("custom notification resolved as Snapchat message: %#v", target)
	}
	history := []localMessage{
		{Author: "  John   Smith "},
		{Author: "You"},
		localMessage{
			Author:       "You",
			IsCustom:     true,
			CustomSender: "John Smith Chat",
		},
	}
	custom := history[2]
	info := buildChatInfo("John Smith Chat", history, "login1")
	sender := messageEventSender(messageSenderName(custom, history), "login1")
	if sender.IsFromMe || sender.Sender != info.Members.OtherUserID ||
		sender.Sender != makeUserID("John Smith") {
		t.Fatalf("custom message sender = %#v, want existing DM recipient %q",
			sender, info.Members.OtherUserID)
	}
}

func TestReceivedSnapNotificationUsesSafeClickableSnapchatLink(t *testing.T) {
	content := localMessageContent(localMessage{
		LinkURL: "https://www.snapchat.com/",
	}, "Alex has sended you a Snap")

	if content.Format != event.FormatHTML {
		t.Fatalf("format = %q, want HTML", content.Format)
	}
	if content.Body != "Alex has sended you a Snap" {
		t.Fatalf("plain body = %q", content.Body)
	}
	if content.FormattedBody != "<a href=\"https://www.snapchat.com/\" style=\"color:inherit;text-decoration:none\"><strong>Alex has sended you a Snap</strong></a>" {
		t.Fatalf("formatted body = %q", content.FormattedBody)
	}
}

func TestNotificationLinkRejectsNonSnapchatTargets(t *testing.T) {
	content := localMessageContent(localMessage{
		LinkURL: "https://example.com/",
	}, "Alex has sended you a Snap")

	if content.Format != "" || content.Body != "Alex has sended you a Snap" ||
		content.FormattedBody != "" {
		t.Fatalf("untrusted notification URL was not ignored: %#v", content)
	}
}

func TestCustomGroupMessageUsesAnExistingChatParticipant(t *testing.T) {
	history := []localMessage{
		{Author: "John"},
		{Author: "Sam"},
		{
			Author:       "You",
			IsCustom:     true,
			CustomSender: "Weekend group",
		},
	}
	customSender := messageEventSender(messageSenderName(history[2], history), "login1")
	info := buildChatInfo("Weekend group", history, "login1")
	if _, exists := info.Members.MemberMap[customSender.Sender]; !exists {
		t.Fatalf("custom message sender %q is not a member of the group", customSender.Sender)
	}
}

func TestCleanupRedactionUsesOriginalMessageSender(t *testing.T) {
	loginID := networkid.UserLoginID("login1")
	affected := []timestampedLocalMessage{
		{Message: localMessage{Author: "You"}},
		{Message: localMessage{Author: "John"}},
	}

	selfSender := messageEventSender(affected[0].Message.Author, loginID)
	remoteSender := messageEventSender(affected[1].Message.Author, loginID)

	if !selfSender.IsFromMe || selfSender.SenderLogin != loginID {
		t.Fatalf("self cleanup sender = %#v, want connected Beeper identity", selfSender)
	}
	if remoteSender.IsFromMe || remoteSender.SenderLogin != "" ||
		remoteSender.Sender != makeUserID("John") {
		t.Fatalf("remote cleanup sender = %#v, want John's Beeper ghost identity", remoteSender)
	}
}

func TestForcedResyncBypassesExistingBridgeMessageSkip(t *testing.T) {
	message := localMessage{BridgeMessageID: "beeper-message-id"}
	if !shouldSkipExistingBridgeMessage(message, -1, false) {
		t.Fatal("normal sync should skip a message already bridged by Beeper")
	}
	if shouldSkipExistingBridgeMessage(message, -1, true) {
		t.Fatal("repair sync should republish an already-bridged message")
	}
	if shouldSkipExistingBridgeMessage(message, 0, false) {
		t.Fatal("a pending outgoing echo should be processed")
	}
}

func TestGetCapabilitiesAdvertisesRepliesAndReactions(t *testing.T) {
	client := &Client{}
	capabilities := client.GetCapabilities(t.Context(), nil)
	if !capabilities.Reply.Full() {
		t.Fatalf("reply capability = %v, want full support", capabilities.Reply)
	}
	if !capabilities.Delete.Full() {
		t.Fatalf("delete capability = %v, want full support", capabilities.Delete)
	}
	if !capabilities.Reaction.Full() || capabilities.ReactionCount != 1 {
		t.Fatalf("reaction capability = %v with count %d, want a one-reaction cap",
			capabilities.Reaction, capabilities.ReactionCount)
	}
	if len(capabilities.AllowedReactions) != len(snapchatReactions) {
		t.Fatalf("allowed reactions = %v, want %v", capabilities.AllowedReactions, snapchatReactions)
	}
}

func TestNormalizeSnapchatReactionAcceptsPresentationVariants(t *testing.T) {
	for _, test := range []struct {
		input string
		want  string
	}{
		{input: "👍️", want: "👍"},
		{input: "👎︎", want: "👎"},
		{input: "❓️", want: "❓"},
	} {
		got, ok := normalizeSnapchatReaction(test.input)
		if !ok || got != test.want {
			t.Errorf("normalizeSnapchatReaction(%q) = %q, %t; want %q, true",
				test.input, got, ok, test.want)
		}
	}
}

func TestGetJSONReportsAPIError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"error":"browser is unavailable"}`))
	}))
	defer server.Close()

	var state localStatus
	err := getJSON(t.Context(), server.Client(), server.URL, &state)
	if err == nil || err.Error() != "local Snapchat API returned HTTP 502: browser is unavailable" {
		t.Fatalf("getJSON() error = %v", err)
	}
}

func TestGetMediaDownloadsTheLocalAttachment(t *testing.T) {
	expected := []byte{0x89, 'P', 'N', 'G'}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/media/42" {
			t.Errorf("media request path = %q, want /api/media/42", r.URL.Path)
		}
		_, _ = w.Write(expected)
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}
	got, err := client.getMedia(t.Context(), 42)
	if err != nil {
		t.Fatalf("getMedia() error = %v", err)
	}
	if string(got) != string(expected) {
		t.Errorf("getMedia() = %v, want %v", got, expected)
	}
}

func TestLocalMessageDecodesMediaOnlyAttachments(t *testing.T) {
	var message localMessage
	if err := json.Unmarshal([]byte(`{
		"body":"Photo",
		"media_only":true,
		"media":[{"id":42,"type":"image","mime_type":"image/png","filename":"photo.png","size":4}]
	}`), &message); err != nil {
		t.Fatalf("unmarshal local message: %v", err)
	}
	if !message.MediaOnly || len(message.Media) != 1 ||
		message.Media[0].ID != 42 || message.Media[0].Type != "image" {
		t.Fatalf("decoded media message = %#v", message)
	}
}

func TestValidateConfigRequiresLoopbackAndValidPolling(t *testing.T) {
	tests := []struct {
		name      string
		config    Config
		wantValid bool
	}{
		{
			name:      "default settings",
			config:    Config{LocalAPI: defaultAPI, PollInterval: "5s"},
			wantValid: true,
		},
		{
			name:      "remote host",
			config:    Config{LocalAPI: "http://example.com:8767", PollInterval: "5s"},
			wantValid: false,
		},
		{
			name:      "poll too frequent",
			config:    Config{LocalAPI: defaultAPI, PollInterval: "100ms"},
			wantValid: false,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := (&Connector{Config: &test.config}).ValidateConfig()
			if (err == nil) != test.wantValid {
				t.Fatalf("ValidateConfig() error = %v, want valid %t", err, test.wantValid)
			}
		})
	}
}

func TestFormatStartupSyncProgress(t *testing.T) {
	got := formatStartupSyncProgress(localStatus{
		Syncing:       true,
		SyncChat:      "John",
		SyncChatIndex: 5,
		SyncChatTotal: 10,
	})
	if want := "Syncing 5/10 chats: John"; got != want {
		t.Fatalf("formatStartupSyncProgress() = %q, want %q", got, want)
	}
}

func TestParseTimestamp(t *testing.T) {
	timestamp, err := parseTimestamp("2026-10-23T01:02:03.123456+00:00")
	if err != nil {
		t.Fatalf("parseTimestamp(): %v", err)
	}
	if timestamp.UTC().Format(time.RFC3339Nano) != "2026-10-23T01:02:03.123456Z" {
		t.Fatalf("parseTimestamp() = %s", timestamp.UTC().Format(time.RFC3339Nano))
	}
}

func TestFindReplyTargetUsesClosestEarlierMessage(t *testing.T) {
	history := []localMessage{
		{
			Author:    "John",
			Body:      "JOKE",
			Timestamp: "2026-10-22T09:58:00Z",
		},
		{
			Author:    "You",
			Body:      "JOKE",
			Timestamp: "2026-10-22T09:59:00Z",
		},
		{
			Author:    "You",
			Body:      "JOKE",
			Timestamp: "2026-10-22T10:01:00Z",
		},
		{
			Author:        "John",
			Body:          "CANCEL",
			Timestamp:     "2026-10-22T10:00:00Z",
			ReplyToAuthor: "Ik",
			ReplyToBody:   "JOKE",
		},
	}

	target := findReplyTarget("chat1", history[3], history)
	want := networkid.MessageID(stableID(stableMessageKey(
		"chat1",
		"You",
		"JOKE",
		time.Date(2026, 10, 22, 9, 59, 0, 0, time.UTC),
	)))
	if target == nil || target.MessageID != want {
		t.Fatalf("findReplyTarget() = %#v, want message ID %q", target, want)
	}
}

func TestFindReplyTargetUsesBeeperMessageIDWhenAvailable(t *testing.T) {
	history := []localMessage{
		{
			Author:          "You",
			Body:            "JOKE",
			Timestamp:       "2026-10-22T09:59:00Z",
			BridgeMessageID: "matrix-outgoing-id",
		},
		{
			Author:        "John",
			Body:          "CANCEL",
			Timestamp:     "2026-10-22T10:00:00Z",
			ReplyToAuthor: "Ik",
			ReplyToBody:   "JOKE",
		},
	}

	target := findReplyTarget("chat1", history[1], history)
	if target == nil || target.MessageID != "matrix-outgoing-id" {
		t.Fatalf("findReplyTarget() = %#v, want Matrix message ID", target)
	}
}

func TestFindReplyTargetDoesNotLinkWhenQuotedAuthorDoesNotMatch(t *testing.T) {
	history := []localMessage{
		{Author: "John", Body: "JOKE", Timestamp: "2026-10-22T09:59:00Z"},
		{
			Author:        "John",
			Body:          "CANCEL",
			Timestamp:     "2026-10-22T10:00:00Z",
			ReplyToAuthor: "Sam",
			ReplyToBody:   "JOKE",
		},
	}

	if target := findReplyTarget("chat1", history[1], history); target != nil {
		t.Fatalf("findReplyTarget() = %#v, want no match", target)
	}
}

func TestLocalChatFlagsAcceptBooleansAndSQLiteIntegers(t *testing.T) {
	for _, test := range []struct {
		payload string
		want    bool
	}{
		{payload: `{"chats":[{"id":"chat1","title":"Enabled","is_group":1,"enabled":1}]}`, want: true},
		{payload: `{"chats":[{"id":"chat1","title":"Disabled","is_group":0,"enabled":0}]}`, want: false},
		{payload: `{"chats":[{"id":"chat1","title":"Enabled","is_group":true,"enabled":true}]}`, want: true},
		{payload: `{"chats":[{"id":"chat1","title":"Disabled","is_group":false,"enabled":false}]}`, want: false},
	} {
		var response localChatsResponse
		if err := json.Unmarshal([]byte(test.payload), &response); err != nil {
			t.Fatalf("unmarshal %s: %v", test.payload, err)
		}
		if len(response.Chats) != 1 ||
			bool(response.Chats[0].Enabled) != test.want ||
			bool(response.Chats[0].IsGroup) != test.want {
			t.Fatalf("unmarshal %s produced chats %#v, want flags=%t", test.payload, response.Chats, test.want)
		}
	}
}

func TestLocalChatEnabledRejectsUnexpectedValue(t *testing.T) {
	var response localChatsResponse
	err := json.Unmarshal([]byte(`{"chats":[{"enabled":2}]}`), &response)
	if err == nil {
		t.Fatal("expected invalid enabled value to fail")
	}
}

func TestLocalMessageReadsBeeperMessageID(t *testing.T) {
	var message localMessage
	if err := json.Unmarshal(
		[]byte(`{"author":"You","body":"JOKE","bridge_message_id":"matrix-outgoing-id"}`),
		&message,
	); err != nil {
		t.Fatalf("unmarshal local message: %v", err)
	}
	if message.BridgeMessageID != "matrix-outgoing-id" {
		t.Fatalf("bridge message ID = %q, want matrix-outgoing-id", message.BridgeMessageID)
	}
}

func TestFindLocalReplyTargetResolvesBridgeAndScrapedMessageIDs(t *testing.T) {
	timestamp := "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	scrapedID := networkid.MessageID(stableID(stableMessageKey(
		"chat1", "John", "hello", parsed,
	)))
	history := []localMessage{
		{
			Author:          "You",
			Body:            "sent from Beeper",
			Timestamp:       timestamp,
			BridgeMessageID: "matrix-outgoing-id",
		},
		{Author: "John", Body: "hello", Timestamp: timestamp},
	}

	for _, test := range []struct {
		messageID networkid.MessageID
		author    string
		body      string
	}{
		{messageID: "matrix-outgoing-id", author: "You", body: "sent from Beeper"},
		{messageID: scrapedID, author: "John", body: "hello"},
	} {
		target := findLocalReplyTarget("chat1", test.messageID, history)
		if target == nil {
			t.Fatalf("findLocalReplyTarget(%q) returned nil", test.messageID)
		}
		if target.Author != test.author || target.Body != test.body || target.Timestamp != timestamp {
			t.Errorf("findLocalReplyTarget(%q) = %#v", test.messageID, target)
		}
	}
	if target := findLocalReplyTarget("chat1", "missing", history); target != nil {
		t.Errorf("findLocalReplyTarget(missing) = %#v, want nil", target)
	}
}

func TestFindLocalReplyTargetDetectsMediaPlaceholderFromSavedAttachment(t *testing.T) {
	const chatID = "chat1"
	const timestamp = "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	messageID := networkid.MessageID(stableID(stableMessageKey(
		chatID, "John", "Photo", parsed,
	)))
	target := findLocalReplyTarget(chatID, messageID, []localMessage{{
		Author:    "John",
		Body:      "Photo",
		Timestamp: timestamp,
		Media:     []localMedia{{Type: "image", MIMEType: "image/jpeg"}},
	}})
	if target == nil {
		t.Fatal("media-only target was not resolved")
	}
	if !target.MediaOnly {
		t.Fatal("saved media attachment was not recognized as a media-only target")
	}
}

func TestFindLocalReplyTargetRecognizesPlaceholderWithoutSavedAttachment(t *testing.T) {
	const chatID = "chat1"
	const timestamp = "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	messageID := networkid.MessageID(stableID(stableMessageKey(
		chatID, "John", "Photo", parsed,
	)))
	target := findLocalReplyTarget(chatID, messageID, []localMessage{{
		Author:    "John",
		Body:      "Photo",
		Timestamp: timestamp,
	}})
	if target == nil {
		t.Fatal("media placeholder target was not resolved without a saved attachment")
	}
	if !target.MediaOnly {
		t.Fatal("media placeholder was not recognized as media-only")
	}
}

func TestFindLocalReplyTargetIncludesEmptyBodyWithMedia(t *testing.T) {
	const chatID = "chat1"
	const timestamp = "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	messageID := networkid.MessageID(stableID(stableMessageKey(
		chatID, "John", "", parsed,
	)))
	target := findLocalReplyTarget(chatID, messageID, []localMessage{{
		Author:    "John",
		Timestamp: timestamp,
		Media:     []localMedia{{Type: "image", MIMEType: "image/jpeg"}},
	}})
	if target == nil {
		t.Fatal("empty-body media target was not resolved")
	}
	if !target.MediaOnly {
		t.Fatal("empty-body message with media was not recognized as media-only")
	}
}

func TestHandleMatrixMessagePassesResolvedReplyTargetToLocalAPI(t *testing.T) {
	timestamp := "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	targetID := networkid.MessageID(stableID(stableMessageKey(
		"chat1", "John", "quoted message", parsed,
	)))
	var request sendMessageRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/messages":
			_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
				Author:    "John",
				Body:      "quoted message",
				Timestamp: timestamp,
				Reactions: []localReaction{{Author: "You", Emoji: "❤️"}},
			}}})
		case "/api/send":
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Errorf("decode send request: %v", err)
			}
			_, _ = w.Write([]byte(`{"status":"sent"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
		pending:   make(map[string][]pendingEcho),
	}
	message, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
			Content: &event.MessageEventContent{
				MsgType: event.MsgText,
				Body:    "my reply",
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		ReplyTo: &database.Message{ID: targetID},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessage() error = %v", err)
	}
	if message == nil || message.DB == nil {
		t.Fatal("HandleMatrixMessage() returned no message response")
	}
	if request.ChatID != "chat1" || request.Body != "my reply" {
		t.Fatalf("send request = %#v", request)
	}
	if request.ReplyTo == nil {
		t.Fatal("send request did not include reply target")
	}
	if request.ReplyTo.Author != "John" ||
		request.ReplyTo.Body != "quoted message" ||
		request.ReplyTo.Timestamp != timestamp {
		t.Errorf("send request reply target = %#v", request.ReplyTo)
	}
}

func TestHandleMatrixMessageRejectsReplyToMediaPart(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		http.Error(w, "unexpected request", http.StatusInternalServerError)
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	response, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
			Content: &event.MessageEventContent{
				MsgType: event.MsgText,
				Body:    "reply",
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		ReplyTo: &database.Message{ID: "snap-message", PartID: "media-2"},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessage() error = %v, want a successful rejection response", err)
	}
	if response == nil || response.DB == nil || response.PostSave == nil {
		t.Fatalf("media reply response = %#v, want a removable saved message", response)
	}
	if requests != 0 {
		t.Fatalf("made %d API requests for a rejected media reply", requests)
	}
}

func TestHandleMatrixMessageRejectsReplyToOutgoingMediaMainPart(t *testing.T) {
	const chatID = "chat1"
	const timestamp = "2026-10-22T09:59:00Z"
	targetID := networkid.MessageID("matrix-outgoing-image")
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path != "/api/messages" {
			http.Error(w, "unexpected request", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
			Author:          "You",
			Body:            "Photo",
			Timestamp:       timestamp,
			BridgeMessageID: string(targetID),
			MediaOnly:       true,
			Media:           []localMedia{{Type: "image", MIMEType: "image/png"}},
		}}})
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	response, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
			Content: &event.MessageEventContent{
				MsgType: event.MsgText,
				Body:    "reply",
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: chatID},
				},
			},
		},
		ReplyTo: &database.Message{ID: targetID, PartID: "main"},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessage() error = %v, want a successful rejection response", err)
	}
	if response == nil || response.DB == nil || response.PostSave == nil {
		t.Fatalf("outgoing media reply response = %#v, want a removable saved message", response)
	}
	if requests != 1 {
		t.Fatalf("made %d API requests, want only one local history lookup", requests)
	}
}

func TestHandleMatrixChatCommandDoesNotSendToSnapchat(t *testing.T) {
	var commands []chatCommandRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/chat-command" {
			t.Errorf("unexpected API request %s; command must not reach Snapchat send", r.URL.Path)
			http.NotFound(w, r)
			return
		}
		var command chatCommandRequest
		if err := json.NewDecoder(r.Body).Decode(&command); err != nil {
			t.Errorf("decode command request: %v", err)
		}
		commands = append(commands, command)
		_ = json.NewEncoder(w).Encode(chatCommandResponse{
			Response: "Open Snapchat",
		})
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}
	for _, commandText := range []string{"!snap", "!streak", "!info"} {
		response, err := client.HandleMatrixMessage(t.Context(), &bridgev2.MatrixMessage{
			MatrixEventBase: bridgev2.MatrixEventBase[*event.MessageEventContent]{
				Content: &event.MessageEventContent{
					MsgType: event.MsgText,
					Body:    commandText,
				},
				Portal: &bridgev2.Portal{
					Portal: &database.Portal{
						PortalKey: networkid.PortalKey{ID: "chat1"},
					},
				},
				InputTransactionID: networkid.RawTransactionID("command-" + commandText),
			},
		})
		if err != nil {
			t.Fatalf("HandleMatrixMessage(%s) error = %v", commandText, err)
		}
		if response == nil || response.DB == nil {
			t.Fatalf("HandleMatrixMessage(%s) returned no command acknowledgment", commandText)
		}
		if response.DB.SenderID != localUserID || response.DB.ID == "" {
			t.Fatalf("command acknowledgment = %#v", response.DB)
		}
		if response.PostSave == nil {
			t.Errorf("HandleMatrixMessage(%s) did not schedule command removal", commandText)
		}
	}
	if len(commands) != 3 {
		t.Fatalf("received %d chat commands, want 3", len(commands))
	}
	for index, commandText := range []string{"!snap", "!streak", "!info"} {
		if commands[index].ChatID != "chat1" || commands[index].Command != commandText {
			t.Errorf("command request = %#v, want chat1/%s", commands[index], commandText)
		}
	}
}

func TestHandleMatrixReactionPassesResolvedTargetToLocalAPI(t *testing.T) {
	timestamp := "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	targetID := networkid.MessageID(stableID(stableMessageKey(
		"chat1", "John", "hello from text", parsed,
	)))
	var requests []sendReactionRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/messages":
			_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
				Author:    "John",
				Body:      "hello from text",
				Timestamp: timestamp,
			}}})
		case "/api/react":
			var request sendReactionRequest
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Errorf("decode reaction request: %v", err)
			}
			requests = append(requests, request)
			_, _ = w.Write([]byte(`{"status":"sent"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}
	matrixReaction := &bridgev2.MatrixReaction{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.ReactionEventContent]{
			Content: &event.ReactionEventContent{
				RelatesTo: event.RelatesTo{
					Type: event.RelAnnotation,
					Key:  "❤️",
				},
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		TargetMessage: &database.Message{ID: targetID, PartID: "main"},
	}
	preResponse, err := client.PreHandleMatrixReaction(t.Context(), matrixReaction)
	if err != nil {
		t.Fatalf("PreHandleMatrixReaction() error = %v", err)
	}
	if preResponse.Emoji != "❤️" || preResponse.MaxReactions != 1 {
		t.Fatalf("reaction pre-response = %#v", preResponse)
	}
	if preResponse.SenderID != localUserID {
		t.Fatalf("reaction sender = %q, want Beeper self identity %q",
			preResponse.SenderID, localUserID)
	}
	reaction, err := client.HandleMatrixReaction(t.Context(), matrixReaction)
	if err != nil {
		t.Fatalf("HandleMatrixReaction() error = %v", err)
	}
	if reaction == nil || reaction.MessageID != targetID || reaction.Emoji != "❤️" {
		t.Fatalf("reaction response = %#v", reaction)
	}
	if len(requests) != 1 {
		t.Fatalf("reaction requests = %d, want one", len(requests))
	}
	request := requests[0]
	if request.ChatID != "chat1" || request.Emoji != "❤️" || request.Target == nil {
		t.Fatalf("reaction request = %#v", request)
	}
	if request.Target.Author != "John" ||
		request.Target.Body != "hello from text" ||
		request.Target.Timestamp != timestamp ||
		request.Target.MediaOnly {
		t.Errorf("reaction target = %#v", request.Target)
	}
	err = client.HandleMatrixReactionRemove(t.Context(), &bridgev2.MatrixReactionRemove{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.RedactionEventContent]{
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		TargetReaction: &database.Reaction{MessageID: targetID, Emoji: "❤️"},
	})
	if err != nil {
		t.Fatalf("HandleMatrixReactionRemove() error = %v", err)
	}
	if len(requests) != 2 || !requests[1].Remove {
		t.Fatalf("reaction removal request = %#v", requests)
	}
}

func TestSendReactionRejectsMediaPartWithoutLocalAPIRequest(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		http.Error(w, "unexpected request", http.StatusInternalServerError)
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	err := client.sendReaction(
		t.Context(), "chat1", "message1", "media-2", "❤️", false,
	)
	if err == nil || !strings.Contains(err.Error(), "media attachments") {
		t.Fatalf("media reaction error = %v, want media-reaction rejection", err)
	}
	if requests != 0 {
		t.Fatalf("made %d API requests for a rejected media reaction", requests)
	}
}

func TestHandleMatrixReactionRejectsMediaPart(t *testing.T) {
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: "http://127.0.0.1:1"}},
		http:      http.DefaultClient,
	}
	reaction, err := client.HandleMatrixReaction(t.Context(), &bridgev2.MatrixReaction{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.ReactionEventContent]{
			Content: &event.ReactionEventContent{
				RelatesTo: event.RelatesTo{
					Type: event.RelAnnotation,
					Key:  "❤️",
				},
			},
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		TargetMessage: &database.Message{ID: "snap-message", PartID: "media-3"},
	})
	if err != nil {
		t.Fatalf("HandleMatrixReaction() error = %v, want a successful rejection response", err)
	}
	if reaction == nil || reaction.MessageID != "snap-message" ||
		reaction.MessagePartID != "media-3" || reaction.Emoji != "❤️" {
		t.Fatalf("media-part reaction = %#v, want a removable rejected reaction", reaction)
	}
}

func TestMediaRejectionNoticeIsItalicAndExact(t *testing.T) {
	tests := []struct {
		name string
		body string
	}{
		{name: "reply", body: mediaReplyRejectionNotice},
		{name: "reaction", body: mediaReactionRejectionNotice},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			content := mediaRejectionNoticeContent(test.body)
			if content.Body != test.body {
				t.Fatalf("notice body = %q, want %q", content.Body, test.body)
			}
			if content.Format != event.FormatHTML || content.FormattedBody != "<em>"+test.body+"</em>" {
				t.Fatalf("formatted notice = (%q, %q), want italic HTML", content.Format, content.FormattedBody)
			}
		})
	}
}

func TestMediaDeletionNoticeLinksOnlyOpenSnapchatText(t *testing.T) {
	content := mediaDeletionNoticeContent()
	if content.Body != mediaDeletionNotice {
		t.Fatalf("notice body = %q, want %q", content.Body, mediaDeletionNotice)
	}
	if content.Format != event.FormatHTML {
		t.Fatalf("notice format = %q, want HTML", content.Format)
	}
	want := `Deleting a Snapchat media attachment is not supported; <a href="https://web.snapchat.com">Open Snapchat instead</a>`
	if content.FormattedBody != want {
		t.Fatalf("formatted notice = %q, want %q", content.FormattedBody, want)
	}
}

func TestHandleMatrixMessageRemoveRejectsMediaPart(t *testing.T) {
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests++
		http.Error(w, "unexpected request", http.StatusInternalServerError)
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	err := client.HandleMatrixMessageRemove(t.Context(), &bridgev2.MatrixMessageRemove{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.RedactionEventContent]{
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		TargetMessage: &database.Message{
			ID:     "snap-message",
			MXID:   "$media-event",
			PartID: "media-1",
		},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessageRemove() error = %v, want media-delete rejection handled", err)
	}
	if requests != 0 {
		t.Fatalf("made %d Snapchat API requests for rejected media deletion", requests)
	}
}

func TestHandleMatrixMessageRemoveRejectsMessageContainingMedia(t *testing.T) {
	const chatID = "chat1"
	const messageID = "snap-message"
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path != "/api/messages" {
			http.Error(w, "unexpected deletion request", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
			Author:          "John",
			Body:            "photo attached",
			Timestamp:       "2026-10-22T09:59:00Z",
			BridgeMessageID: messageID,
			Media:           []localMedia{{Type: "image", MIMEType: "image/png"}},
		}}})
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	err := client.HandleMatrixMessageRemove(t.Context(), &bridgev2.MatrixMessageRemove{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.RedactionEventContent]{
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: chatID},
				},
			},
		},
		TargetMessage: &database.Message{
			ID:     messageID,
			MXID:   "$main-event",
			PartID: "main",
		},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessageRemove() error = %v, want media-delete rejection handled", err)
	}
	if requests != 1 {
		t.Fatalf("made %d API requests, want history lookup only", requests)
	}
}

func TestSendReactionRejectsMediaOnlyMainPart(t *testing.T) {
	const chatID = "chat1"
	const timestamp = "2026-10-22T09:59:00Z"
	const messageID = networkid.MessageID("matrix-outgoing-image")
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path != "/api/messages" {
			http.Error(w, "unexpected reaction request", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
			Author:          "You",
			Body:            "Photo",
			Timestamp:       timestamp,
			BridgeMessageID: string(messageID),
			MediaOnly:       true,
			Media:           []localMedia{{Type: "image", MIMEType: "image/png"}},
		}}})
	}))
	defer server.Close()
	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}

	err := client.sendReaction(t.Context(), chatID, messageID, "main", "❤️", false)
	if err == nil || !strings.Contains(err.Error(), "media-only") {
		t.Fatalf("media-only reaction error = %v, want media-only rejection", err)
	}
	if requests != 1 {
		t.Fatalf("made %d API requests, want only one local history lookup", requests)
	}
}

func TestMessageRemovalTargetsOnlyDeletedMessageMedia(t *testing.T) {
	parsed, err := parseTimestamp("2026-10-22T10:00:00Z")
	if err != nil {
		t.Fatal(err)
	}
	target := localMessage{
		Author:    "John",
		Body:      "deleted",
		Timestamp: "2026-10-22T10:00:00Z",
	}
	for index := 0; index < 11; index++ {
		target.Media = append(target.Media, localMedia{
			ID:       int64(index + 1),
			MIMEType: "image/jpeg",
			Filename: fmt.Sprintf("image-%d.jpg", index),
		})
	}
	targetID := localMessageID("chat1", target, parsed)
	targets := messageRemovalTargets("chat1", timestampedLocalMessage{
		Message:   target,
		ID:        targetID,
		Timestamp: parsed,
	}, targetID)

	wantIDs := map[networkid.MessageID]bool{
		networkid.MessageID(stableID(fmt.Sprintf("%s\x00media-batch\x001", targetID))): true,
	}
	messageKey := stableMessageKey("chat1", target.Author, target.Body, parsed)
	mediaID := networkid.MessageID(stableID(
		messageKey + "\x00media\x00" + mediaSeenKey(messageKey, target.Media),
	))
	wantIDs[mediaID] = true
	wantIDs[networkid.MessageID(stableID(fmt.Sprintf(
		"%s\x00media-batch\x001", mediaID,
	)))] = true

	if len(targets) != len(wantIDs) {
		t.Fatalf("removal target count = %d, want %d: %#v", len(targets), len(wantIDs), targets)
	}
	for _, removal := range targets {
		if !wantIDs[removal.ID] {
			t.Errorf("unexpected removal target %q", removal.ID)
		}
		delete(wantIDs, removal.ID)
	}
	for id := range wantIDs {
		t.Errorf("missing removal target %q", id)
	}

	newerTimestamp := parsed.Add(time.Second)
	newer := target
	newer.Author = "Alex"
	newer.Body = "keep this newer media"
	newer.Timestamp = newerTimestamp.Format(time.RFC3339)
	newerID := localMessageID("chat1", newer, newerTimestamp)
	newerTargets := messageRemovalTargets("chat1", timestampedLocalMessage{
		Message:   newer,
		ID:        newerID,
		Timestamp: newerTimestamp,
	}, targetID)
	for _, newerTarget := range newerTargets {
		for _, removedTarget := range targets {
			if newerTarget.ID == removedTarget.ID {
				t.Errorf("newer media removal %q collides with deleted media", newerTarget.ID)
			}
		}
	}
}

func TestHandleMatrixMessageRemovePassesResolvedTargetToLocalAPI(t *testing.T) {
	const timestamp = "2026-10-22T09:59:00Z"
	parsed, err := parseTimestamp(timestamp)
	if err != nil {
		t.Fatal(err)
	}
	targetID := networkid.MessageID(stableID(stableMessageKey(
		"chat1", "John", "message to delete", parsed,
	)))
	var request deleteMessageRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/messages":
			_ = json.NewEncoder(w).Encode(localMessagesResponse{Messages: []localMessage{{
				Author:    "John",
				Body:      "message to delete",
				Timestamp: timestamp,
			}}})
		case "/api/delete-message":
			if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
				t.Errorf("decode delete request: %v", err)
			}
			_, _ = w.Write([]byte(`{"status":"deleted"}`))
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	client := &Client{
		connector: &Connector{Config: &Config{LocalAPI: server.URL}},
		http:      server.Client(),
	}
	err = client.HandleMatrixMessageRemove(t.Context(), &bridgev2.MatrixMessageRemove{
		MatrixEventBase: bridgev2.MatrixEventBase[*event.RedactionEventContent]{
			Portal: &bridgev2.Portal{
				Portal: &database.Portal{
					PortalKey: networkid.PortalKey{ID: "chat1"},
				},
			},
		},
		TargetMessage: &database.Message{ID: targetID},
	})
	if err != nil {
		t.Fatalf("HandleMatrixMessageRemove() error = %v", err)
	}
	if request.ChatID != "chat1" || request.Target == nil {
		t.Fatalf("delete request = %#v", request)
	}
	if request.Target.Author != "John" ||
		request.Target.Body != "message to delete" ||
		request.Target.Timestamp != timestamp {
		t.Fatalf("delete target = %#v", request.Target)
	}
}

func TestPortalKeyIncludesReceiverLogin(t *testing.T) {
	client := &Client{login: &bridgev2.UserLogin{UserLogin: &database.UserLogin{
		ID: networkid.UserLoginID("local-account"),
	}}}

	key := client.portalKey("chat-123")
	if key.ID != networkid.PortalID("chat-123") {
		t.Fatalf("portal ID = %q, want chat-123", key.ID)
	}
	if key.Receiver != networkid.UserLoginID("local-account") {
		t.Fatalf("portal receiver = %q, want local-account", key.Receiver)
	}
}

func TestBuildChatInfoMarksSingleRecipientAsDirectChat(t *testing.T) {
	info := buildChatInfo("Anna daniels", []localMessage{
		{Author: "You", Body: "Hi"},
		{Author: "Anna daniels", Body: "Hello"},
		{Author: "Snapchat", Body: "Snapstreak", IsCustom: true},
	}, "local-account")

	if info.Type == nil || *info.Type != database.RoomTypeDM {
		t.Fatalf("room type = %v, want DM", info.Type)
	}
	if info.Members == nil || info.Members.OtherUserID != makeUserID("Anna daniels") {
		t.Fatalf("DM recipient = %v, want %q", info.Members, makeUserID("Anna daniels"))
	}
	if len(info.Members.MemberMap) != 2 || !info.Members.IsFull {
		t.Fatalf("DM members = %#v, want full two-member list", info.Members)
	}
}

func TestBuildChatInfoUsesBitmojiOnlyForDirectChats(t *testing.T) {
	avatarURL := "https://cf-st.sc-cdn.net/3d/render/avatar.webp?trim=circle&ua=2"
	direct := buildChatInfoWithAvatar("Anna", []localMessage{
		{Author: "Anna"},
	}, "local-account", avatarURL)
	if direct.Avatar == nil || direct.Avatar.ID == "" {
		t.Fatal("direct chat did not receive its available Bitmoji avatar")
	}

	group := buildChatInfoWithAvatar("Weekend group", []localMessage{
		{Author: "Anna"},
		{Author: "John"},
	}, "local-account", avatarURL)
	if group.Avatar != nil {
		t.Fatal("group chat received a Bitmoji avatar")
	}
}

func TestBuildChatInfoKeepsEmptyGroupWithoutAvatar(t *testing.T) {
	info := buildChatInfoForChat(localChat{
		ID:            "group1",
		Title:         "Weekend",
		AvatarURL:     "https://cf-st.sc-cdn.net/3d/render/group.webp",
		AvatarVersion: "2026-10-27T03:00:00+00:00",
		IsGroup:       true,
	}, nil, "local-account")

	if info.Type == nil || *info.Type != database.RoomTypeGroupDM {
		t.Fatalf("room type = %v, want group DM without message history", info.Type)
	}
	if info.Avatar != nil {
		t.Fatal("empty group unexpectedly received an avatar")
	}
}

func TestBitmojiAvatarURLRequiresSnapchatRenderHost(t *testing.T) {
	for _, testCase := range []struct {
		url   string
		valid bool
	}{
		{"https://cf-st.sc-cdn.net/3d/render/avatar.webp", true},
		{"http://cf-st.sc-cdn.net/3d/render/avatar.webp", false},
		{"https://cf-st.sc-cdn.net.evil.example/3d/render/avatar.webp", false},
		{"https://user@cf-st.sc-cdn.net/3d/render/avatar.webp", false},
		{"https://cf-st.sc-cdn.net/avatar.webp", false},
		{"https://cf-st.sc-cdn.net:443/3d/render/avatar.webp", false},
	} {
		if got := isBitmojiAvatarURL(testCase.url); got != testCase.valid {
			t.Errorf("isBitmojiAvatarURL(%q) = %t, want %t", testCase.url, got, testCase.valid)
		}
	}
}

func TestShouldUpdateDirectChatWhenBitmojiBecomesAvailable(t *testing.T) {
	info := buildChatInfoWithAvatar("Anna", []localMessage{
		{Author: "Anna"},
	}, "local-account", "https://cf-st.sc-cdn.net/3d/render/avatar.webp")
	portal := &bridgev2.Portal{Portal: &database.Portal{
		Name:        "Anna",
		RoomType:    database.RoomTypeDM,
		OtherUserID: makeUserID("Anna"),
	}}

	if !shouldUpdateChatInfo(info, portal) {
		t.Fatal("direct chat with a newly available Bitmoji avatar was not marked for update")
	}
}

func TestBitmojiVersionForcesDailyAvatarResync(t *testing.T) {
	messages := []localMessage{{Author: "Anna"}}
	avatarURL := "https://cf-st.sc-cdn.net/3d/render/avatar.webp"
	first := buildChatInfoWithAvatarVersion(
		"Anna", messages, "local-account", avatarURL, "2026-10-26T03:00:00+00:00",
	)
	nextDay := buildChatInfoWithAvatarVersion(
		"Anna", messages, "local-account", avatarURL, "2026-10-27T03:00:00+00:00",
	)

	if first.Avatar.ID == nextDay.Avatar.ID {
		t.Fatal("avatar ID did not change for the daily refresh")
	}
}

func TestBuildChatInfoDeduplicatesParticipantNameWhitespaceAndCase(t *testing.T) {
	info := buildChatInfo("John", []localMessage{
		{Author: "John"},
		{Author: " john "},
		{Author: "You"},
	}, "local-account")

	if info.Type == nil || *info.Type != database.RoomTypeDM {
		t.Fatalf("room type = %v, want direct chat", info.Type)
	}
	if got := len(info.Members.MemberMap); got != 2 {
		t.Fatalf("member count = %d, want @me and one participant", got)
	}
	if info.Members.OtherUserID != makeUserID("John") {
		t.Fatalf("other user = %q, want canonical John identity", info.Members.OtherUserID)
	}
}

func TestBuildChatInfoKeepsMultipleRecipientsAsGroupDM(t *testing.T) {
	info := buildChatInfo("Friend group", []localMessage{
		{Author: "Anna"},
		{Author: "John"},
	}, "local-account")

	if info.Type == nil || *info.Type != database.RoomTypeGroupDM {
		t.Fatalf("room type = %v, want group DM", info.Type)
	}
	if info.Members.OtherUserID != "" {
		t.Fatalf("group DM has other user %q", info.Members.OtherUserID)
	}
	if info.Members.IsFull {
		t.Fatal("group DM member list should not be treated as complete")
	}
	if len(info.Members.MemberMap) != 3 {
		t.Fatalf("group DM has %d members, want self and two recipients", len(info.Members.MemberMap))
	}
	for _, name := range []string{"Anna", "John"} {
		member, exists := info.Members.MemberMap[makeUserID(name)]
		if !exists || member.UserInfo == nil || member.UserInfo.Name == nil ||
			*member.UserInfo.Name != name {
			t.Errorf("member %q has profile %#v, want their own name", name, member.UserInfo)
		}
	}
}

func TestExistingGroupPortalDoesNotBecomeADirectChatWithOneVisibleSender(t *testing.T) {
	info := buildChatInfo("Weekend group", []localMessage{
		{Author: "Anna"},
	}, "local-account")
	portal := &bridgev2.Portal{Portal: &database.Portal{
		Name:     "Weekend group",
		RoomType: database.RoomTypeGroupDM,
	}}

	if shouldUpdateChatInfo(info, portal) {
		t.Fatal("stable group portal unexpectedly needs an update")
	}
	if info.Type == nil || *info.Type != database.RoomTypeGroupDM {
		t.Fatalf("room type = %v, want preserved group DM", info.Type)
	}
	if info.Members.OtherUserID != "" {
		t.Fatalf("group portal kept direct-chat recipient %q", info.Members.OtherUserID)
	}
	if info.Members.IsFull {
		t.Fatal("partial sender history must not be treated as a full group member list")
	}
}

func TestExistingGroupPortalRemovesItsAvatar(t *testing.T) {
	info := buildChatInfoWithAvatar("Weekend group", []localMessage{
		{Author: "Anna"},
		{Author: "John"},
	}, "local-account", "https://cf-st.sc-cdn.net/3d/render/avatar.webp")
	portal := &bridgev2.Portal{Portal: &database.Portal{
		Name:      "Weekend group",
		RoomType:  database.RoomTypeGroupDM,
		AvatarSet: true,
		AvatarMXC: "mxc://example.com/group-avatar",
	}}

	if !shouldUpdateChatInfo(info, portal) {
		t.Fatal("existing group avatar was not scheduled for removal")
	}
	if info.Avatar == nil || !info.Avatar.Remove {
		t.Fatal("existing group avatar was not removed")
	}
	if !info.ExcludeChangesFromTimeline {
		t.Fatal("group-avatar removal should not create a Beeper timeline notification")
	}
}

func TestExistingGroupPortalUpdatesItsNameWhenSnapchatNameChanges(t *testing.T) {
	info := buildChatInfo("Weekend group", []localMessage{
		{Author: "Anna"},
		{Author: "John"},
	}, "local-account")
	portal := &bridgev2.Portal{Portal: &database.Portal{
		Name:     "Anna",
		RoomType: database.RoomTypeGroupDM,
	}}

	if !shouldUpdateChatInfo(info, portal) {
		t.Fatal("renamed group portal should update")
	}
	if *info.Name != "Weekend group" {
		t.Fatalf("group name = %q, want Snapchat group title", *info.Name)
	}
}

func TestBuildChatInfoUsesTitleWhenNoRemoteMessagesExist(t *testing.T) {
	info := buildChatInfo("Jess", []localMessage{{Author: "You"}}, "local-account")

	if info.Type == nil || *info.Type != database.RoomTypeDM {
		t.Fatalf("room type = %v, want DM", info.Type)
	}
	if info.Members.OtherUserID != makeUserID("Jess") {
		t.Fatalf("DM recipient = %q, want %q", info.Members.OtherUserID, makeUserID("Jess"))
	}
}
