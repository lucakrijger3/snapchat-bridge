package snapchat

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"path"
	"strings"
	"sync"
	"time"

	"go.mau.fi/util/configupgrade"

	"maunium.net/go/mautrix/bridgev2"
	"maunium.net/go/mautrix/bridgev2/database"
	"maunium.net/go/mautrix/bridgev2/networkid"
	"maunium.net/go/mautrix/bridgev2/simplevent"
	"maunium.net/go/mautrix/bridgev2/status"
	"maunium.net/go/mautrix/event"
	"maunium.net/go/mautrix/id"
)

const (
	networkID               = "snapchat"
	localUserID             = networkid.UserID("self")
	defaultAPI              = "http://127.0.0.1:8767"
	defaultPoll             = 5 * time.Second
	maxResponseLen          = 16 << 20
	maxMediaSize            = 50 << 20
	maxBeeperMediaParts     = 10
	maxQueuedOutgoingImages = 10
	maxOutgoingImageSize    = 40 << 20
	maxAvatarSize           = 10 << 20
)

const (
	outgoingImageRequestTimeout = 8 * time.Minute
)

const (
	mediaReplyRejectionNotice    = "Replying to Snapchat media attachments is not supported; reply to a text message instead"
	mediaReactionRejectionNotice = "Reacting to Snapchat media attachments is not supported; react to a text message instead"
	mediaDeletionNotice          = "Deleting a Snapchat media attachment is not supported; Open Snapchat instead"
)

var snapchatReactions = []string{"❤️", "😂", "🔥", "👍", "👎", "😢", "🤯", "❓"}

type mediaInteractionRejection struct {
	message string
	notice  string
}

func (rejection *mediaInteractionRejection) Error() string {
	return rejection.message
}

type Config struct {
	LocalAPI     string `yaml:"local_api"`
	PollInterval string `yaml:"poll_interval"`
}

type Connector struct {
	Config          *Config
	Bridge          *bridgev2.Bridge
	AutoLoginUserID id.UserID
}

func NewConnector() *Connector {
	return &Connector{Config: &Config{
		LocalAPI:     defaultAPI,
		PollInterval: defaultPoll.String(),
	}}
}

func (c *Connector) Init(br *bridgev2.Bridge) {
	c.Bridge = br
}

func (c *Connector) Start(context.Context) error {
	if err := c.ValidateConfig(); err != nil {
		return err
	}
	if c.AutoLoginUserID != "" {
		userID := string(c.AutoLoginUserID)
		if !strings.HasPrefix(userID, "@") || !strings.Contains(userID, ":") {
			return fmt.Errorf("automatic login user ID must be a Matrix user ID")
		}
	}
	return nil
}

func (c *Connector) StartAutomaticLogin() {
	if c.AutoLoginUserID != "" {
		go c.startAutomaticLogin(c.Bridge.BackgroundCtx)
	}
}

func (c *Connector) startAutomaticLogin(ctx context.Context) {
	user, err := c.Bridge.GetUserByMXID(ctx, c.AutoLoginUserID)
	if err != nil {
		c.Bridge.Log.Err(err).Msg("Failed to load user for automatic Snapchat login")
		return
	}
	if len(user.GetUserLogins()) > 0 {
		c.Bridge.Log.Info().Msg("Skipping automatic Snapchat login because the user is already logged in")
		return
	}

	if err := c.waitForStartupSync(ctx); err != nil {
		if ctx.Err() == nil {
			c.Bridge.Log.Err(err).Msg("Automatic Snapchat login could not proceed after checking the initial local sync")
		}
		return
	}

	process, err := c.CreateLogin(ctx, user, "local")
	if err != nil {
		c.Bridge.Log.Err(err).Msg("Failed to create automatic Snapchat login")
		return
	}
	step, err := process.Start(ctx)
	if err != nil {
		c.Bridge.Log.Err(err).Msg("Failed to start automatic Snapchat login")
		return
	}
	if step == nil || step.Type != bridgev2.LoginStepTypeComplete {
		c.Bridge.Log.Error().Msg("Automatic Snapchat login did not complete")
		return
	}
	c.Bridge.Log.Info().Msg("Automatically connected the local Snapchat session")
}

func (c *Connector) waitForStartupSync(ctx context.Context) error {
	client := &http.Client{Timeout: 10 * time.Second}
	endpoint := strings.TrimRight(c.Config.LocalAPI, "/") + "/api/status"
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	lastProgress := ""
	c.Bridge.Log.Info().Msg("Waiting for the local app's initial chat sync")

	for {
		var state localStatus
		if err := getJSON(ctx, client, endpoint, &state); err != nil {
			return fmt.Errorf("could not check local Snapchat sync status: %w", err)
		}
		if state.StartupSyncComplete == nil {
			return fmt.Errorf("the local app does not report startup sync completion; restart app.py")
		}
		if state.StartupSyncError != "" {
			return fmt.Errorf("initial local Snapchat sync failed: %s", state.StartupSyncError)
		}
		if *state.StartupSyncComplete && !state.Syncing {
			c.Bridge.Log.Info().Msg("Initial Snapchat chat sync is complete")
			return nil
		}
		if progress := formatStartupSyncProgress(state); progress != "" {
			if progress != lastProgress {
				c.Bridge.Log.Info().Msg(progress)
				lastProgress = progress
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func formatStartupSyncProgress(state localStatus) string {
	if !state.Syncing || state.SyncChatIndex < 1 || state.SyncChatTotal < 1 {
		return ""
	}
	return fmt.Sprintf(
		"Syncing %d/%d chats: %s",
		state.SyncChatIndex,
		state.SyncChatTotal,
		state.SyncChat,
	)
}

func (c *Connector) ValidateConfig() error {
	parsed, err := url.Parse(c.Config.LocalAPI)
	if err != nil || parsed.Scheme != "http" || parsed.Host == "" {
		return fmt.Errorf("network.local_api must be an http URL")
	}
	host := parsed.Hostname()
	if host != "localhost" && net.ParseIP(host).IsLoopback() == false {
		return fmt.Errorf("network.local_api must point to localhost or a loopback IP")
	}
	interval, err := time.ParseDuration(c.Config.PollInterval)
	if err != nil || interval < time.Second {
		return fmt.Errorf("network.poll_interval must be a duration of at least 1s")
	}
	return nil
}

func (c *Connector) GetName() bridgev2.BridgeName {
	return bridgev2.BridgeName{
		DisplayName:          "Snapchat",
		NetworkURL:           "https://www.snapchat.com/",
		NetworkID:            networkID,
		BeeperBridgeType:     "github.com/lucakrijger3/sh-snapchat",
		DefaultPort:          29347,
		DefaultCommandPrefix: "!snapchat",
	}
}

func (c *Connector) GetDBMetaTypes() database.MetaTypes {
	return database.MetaTypes{}
}

func (c *Connector) GetCapabilities() *bridgev2.NetworkGeneralCapabilities {
	return &bridgev2.NetworkGeneralCapabilities{}
}

func (c *Connector) GetConfig() (string, any, configupgrade.Upgrader) {
	example := "local_api: http://127.0.0.1:8767\npoll_interval: 5s"
	return example, c.Config, configupgrade.NoopUpgrader
}

func (c *Connector) LoadUserLogin(_ context.Context, login *bridgev2.UserLogin) error {
	login.Client = &Client{
		connector:          c,
		login:              login,
		http:               &http.Client{Timeout: outgoingImageRequestTimeout},
		seen:               make(map[string]struct{}),
		seenMedia:          make(map[string]struct{}),
		seenReactions:      make(map[string]struct{}),
		seenReadReceipts:   make(map[string]networkid.MessageID),
		replayMessages:     make(map[string]struct{}),
		refreshPortals:     make(map[string]struct{}),
		queuedRemovals:     make(map[string]struct{}),
		pending:            make(map[string][]pendingEcho),
		refreshInfoPending: true,
		typing:             make(map[string]remoteTypingState),
	}
	return nil
}

func (c *Connector) GetLoginFlows() []bridgev2.LoginFlow {
	return []bridgev2.LoginFlow{{
		Name:        "Use local Snapchat session",
		Description: "Connects the Snapchat account already signed in through the local Snapchat app.",
		ID:          "local",
	}}
}

func (c *Connector) CreateLogin(_ context.Context, user *bridgev2.User, flowID string) (bridgev2.LoginProcess, error) {
	if flowID != "local" {
		return nil, fmt.Errorf("unknown login flow %q", flowID)
	}
	return &loginProcess{connector: c, user: user}, nil
}

func (c *Connector) GetBridgeInfoVersion() (int, int) {
	return 1, 2
}

type loginProcess struct {
	connector *Connector
	user      *bridgev2.User
}

func (p *loginProcess) Start(ctx context.Context) (*bridgev2.LoginStep, error) {
	client := &http.Client{Timeout: 10 * time.Second}
	var state localStatus
	if err := getJSON(ctx, client, strings.TrimRight(p.connector.Config.LocalAPI, "/")+"/api/status", &state); err != nil {
		return nil, fmt.Errorf("could not contact the local Snapchat app: %w", err)
	}
	if !state.LoggedIn {
		return nil, fmt.Errorf("the local Snapchat app is not signed in; start it and finish Snapchat login first")
	}

	loginID := networkid.UserLoginID("local-" + stableID(string(p.user.MXID))[:16])
	login, err := p.user.NewLogin(ctx, &database.UserLogin{
		ID:         loginID,
		RemoteName: "Snapchat",
	}, nil)
	if err != nil {
		return nil, fmt.Errorf("could not create Snapchat login: %w", err)
	}
	login.Client.Connect(login.Log.WithContext(p.connector.Bridge.BackgroundCtx))
	return &bridgev2.LoginStep{
		Type: bridgev2.LoginStepTypeComplete,
		CompleteParams: &bridgev2.LoginCompleteParams{
			UserLoginID: login.ID,
			UserLogin:   login,
		},
	}, nil
}

func (p *loginProcess) Cancel() {}

type Client struct {
	connector *Connector
	login     *bridgev2.UserLogin
	http      *http.Client

	mu                 sync.Mutex
	outgoingImageMu    sync.Mutex
	online             bool
	cancel             context.CancelFunc
	seen               map[string]struct{}
	seenMedia          map[string]struct{}
	seenReactions      map[string]struct{}
	seenReadReceipts   map[string]networkid.MessageID
	replayMessages     map[string]struct{}
	outgoingImageQueue map[string]map[string]struct{}
	refreshPortals     map[string]struct{}
	queuedRemovals     map[string]struct{}
	pending            map[string][]pendingEcho
	typing             map[string]remoteTypingState
	refreshInfoPending bool
	last               status.BridgeStateEvent
	lastMsg            string
	lastSyncError      string
}

type pendingEcho struct {
	id      networkid.MessageID
	created time.Time
}

type remoteTypingState struct {
	sender    bridgev2.EventSender
	delivered bool
}

type localStatus struct {
	LoggedIn            bool     `json:"logged_in"`
	Syncing             bool     `json:"syncing"`
	SyncChat            string   `json:"sync_chat"`
	SyncChatIndex       int      `json:"sync_chat_index"`
	SyncChatTotal       int      `json:"sync_chat_total"`
	StartupSyncComplete *bool    `json:"startup_sync_complete"`
	StartupSyncError    string   `json:"startup_sync_error"`
	TypingChats         []string `json:"typing_chats"`
	OpenedChats         []string `json:"opened_chats"`
}

type localChatsResponse struct {
	Chats []localChat `json:"chats"`
}

type localChat struct {
	ID            string      `json:"id"`
	Title         string      `json:"title"`
	AvatarURL     string      `json:"avatar_url"`
	AvatarVersion string      `json:"avatar_version"`
	IsGroup       enabledFlag `json:"is_group"`
	Enabled       enabledFlag `json:"enabled"`
}

type enabledFlag bool

func (enabled *enabledFlag) UnmarshalJSON(data []byte) error {
	switch string(data) {
	case "true", "1":
		*enabled = true
	case "false", "0":
		*enabled = false
	default:
		return fmt.Errorf("enabled must be a boolean or 0/1, got %s", data)
	}
	return nil
}

type localMessagesResponse struct {
	Messages []localMessage `json:"messages"`
}

type localMessageRemovalsResponse struct {
	Removals []localMessageRemoval `json:"removals"`
}

type localMessageRemoval struct {
	ChatID    string `json:"chat_id"`
	CustomID  string `json:"custom_id"`
	Timestamp string `json:"timestamp"`
}

type localMessage struct {
	Author          string          `json:"author"`
	CustomSender    string          `json:"custom_sender"`
	Body            string          `json:"body"`
	Timestamp       string          `json:"timestamp"`
	Status          string          `json:"status"`
	IsCustom        bool            `json:"is_custom"`
	CustomID        string          `json:"custom_id"`
	ReplyToAuthor   string          `json:"reply_to_author"`
	ReplyToBody     string          `json:"reply_to_body"`
	BridgeMessageID string          `json:"bridge_message_id"`
	Reactions       []localReaction `json:"reactions"`
	Media           []localMedia    `json:"media"`
	MediaOnly       bool            `json:"media_only"`
	LinkURL         string          `json:"link_url"`
}

type localMedia struct {
	ID       int64  `json:"id"`
	Type     string `json:"type"`
	MIMEType string `json:"mime_type"`
	Filename string `json:"filename"`
	Size     int64  `json:"size"`
}

type localReaction struct {
	Author string `json:"author"`
	Emoji  string `json:"emoji"`
}

type localReplyTarget struct {
	Author    string `json:"author"`
	Body      string `json:"body"`
	Timestamp string `json:"timestamp"`
	MediaOnly bool   `json:"media_only,omitempty"`
}

type sendMessageRequest struct {
	ChatID          string            `json:"chat_id"`
	Body            string            `json:"body"`
	BridgeMessageID string            `json:"bridge_message_id"`
	ReplyTo         *localReplyTarget `json:"reply_to,omitempty"`
}

type chatCommandRequest struct {
	ChatID  string `json:"chat_id"`
	Command string `json:"command"`
}

type chatCommandResponse struct {
	Response string `json:"response"`
}

type deleteMessageRequest struct {
	ChatID string            `json:"chat_id"`
	Target *localReplyTarget `json:"target"`
}

type sendReactionRequest struct {
	ChatID string            `json:"chat_id"`
	Target *localReplyTarget `json:"target"`
	Emoji  string            `json:"emoji"`
	Remove bool              `json:"remove,omitempty"`
}

func (c *Client) Connect(ctx context.Context) {
	c.mu.Lock()
	if c.cancel != nil {
		c.cancel()
	}
	clientCtx, cancel := context.WithCancel(ctx)
	c.cancel = cancel
	c.mu.Unlock()
	go c.syncLoop(clientCtx)
}

func (c *Client) Disconnect() {
	c.mu.Lock()
	if c.cancel != nil {
		c.cancel()
		c.cancel = nil
	}
	c.online = false
	c.mu.Unlock()
}

func (c *Client) IsLoggedIn() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.online
}

func (c *Client) LogoutRemote(context.Context) {
	c.Disconnect()
}

func (c *Client) IsThisUser(_ context.Context, userID networkid.UserID) bool {
	return userID == localUserID
}

func (c *Client) GetChatInfo(ctx context.Context, portal *bridgev2.Portal) (*bridgev2.ChatInfo, error) {
	chats, err := c.getChats(ctx)
	if err != nil {
		return nil, err
	}
	for _, chat := range chats {
		if chat.ID == string(portal.PortalKey.ID) {
			messages, err := c.getMessages(ctx, chat.ID)
			if err != nil {
				return nil, fmt.Errorf("could not read messages for %q: %w", chat.Title, err)
			}
			return buildChatInfoForChat(chat, messages, c.login.ID), nil
		}
	}
	return nil, fmt.Errorf("Snapchat chat %q is no longer available", portal.PortalKey.ID)
}

func (c *Client) GetUserInfo(_ context.Context, ghost *bridgev2.Ghost) (*bridgev2.UserInfo, error) {
	encoded := strings.TrimPrefix(string(ghost.ID), "user:")
	name, err := decodeUserName(encoded)
	if err != nil {
		return nil, fmt.Errorf("invalid Snapchat ghost ID %q: %w", ghost.ID, err)
	}
	displayName := name
	return &bridgev2.UserInfo{Name: &displayName}, nil
}

func (c *Client) GetCapabilities(context.Context, *bridgev2.Portal) *event.RoomFeatures {
	return &event.RoomFeatures{
		File: event.FileFeatureMap{
			event.MsgImage: &event.FileFeatures{
				MimeTypes: map[string]event.CapabilitySupportLevel{
					"image/png":  event.CapLevelFullySupported,
					"image/jpeg": event.CapLevelFullySupported,
					"image/gif":  event.CapLevelFullySupported,
				},
				MaxSize: maxOutgoingImageSize,
			},
		},
		Reply:            event.CapLevelFullySupported,
		Delete:           event.CapLevelFullySupported,
		Reaction:         event.CapLevelFullySupported,
		ReactionCount:    1,
		AllowedReactions: append([]string(nil), snapchatReactions...),
	}
}

func (c *Client) HandleMatrixMessage(ctx context.Context, msg *bridgev2.MatrixMessage) (*bridgev2.MatrixMessageResponse, error) {
	if msg.Content == nil {
		return nil, fmt.Errorf("Snapchat message has no content")
	}
	if msg.Content.MsgType == event.MsgImage {
		return c.handleMatrixImage(ctx, msg)
	}
	if msg.Content.MsgType == event.MsgAudio {
		return nil, fmt.Errorf("Snapchat Web does not support sending audio messages")
	}
	if msg.Content.MsgType != event.MsgText {
		return nil, fmt.Errorf("Snapchat bridge supports text and image messages only")
	}
	body := strings.TrimSpace(msg.Content.Body)
	if body == "" {
		return nil, fmt.Errorf("message body must not be empty")
	}

	chatID := string(msg.Portal.PortalKey.ID)
	transactionID := string(msg.InputTransactionID)
	if transactionID == "" && msg.Event != nil {
		transactionID = string(msg.Event.ID)
	}
	if transactionID == "" {
		transactionID = fmt.Sprintf("%s\x00%s\x00%d", chatID, body, time.Now().UnixNano())
	}
	command := strings.ToLower(body)
	if command == "!snap" || command == "!streak" || command == "!info" {
		var result chatCommandResponse
		if err := postJSON(
			ctx,
			c.http,
			c.apiURL()+"/api/chat-command",
			chatCommandRequest{ChatID: chatID, Command: command},
			&result,
		); err != nil {
			return nil, fmt.Errorf("could not handle Snapchat chat command: %w", err)
		}
		return &bridgev2.MatrixMessageResponse{
			DB: &database.Message{
				ID:        networkid.MessageID("command-" + stableID(transactionID)),
				PartID:    "main",
				Room:      msg.Portal.PortalKey,
				SenderID:  localUserID,
				Timestamp: time.Now(),
			},
			PostSave: func(_ context.Context, saved *database.Message) {
				result := c.login.QueueRemoteEvent(&simplevent.MessageRemove{
					EventMeta: simplevent.EventMeta{
						Type:      bridgev2.RemoteEventMessageRemove,
						PortalKey: c.portalKey(chatID),
						Sender:    messageEventSender("You", c.login.ID),
						Timestamp: saved.Timestamp,
					},
					TargetMessage:   saved.ID,
					HidePlaceholder: true,
				})
				if result.Error != nil {
					c.connector.Bridge.Log.Err(result.Error).
						Str("chat_id", chatID).
						Str("message_id", string(saved.ID)).
						Msg("Could not remove Snapchat chat command from Beeper")
				} else if !result.Success {
					c.connector.Bridge.Log.Error().
						Str("chat_id", chatID).
						Str("message_id", string(saved.ID)).
						Msg("Could not queue removal of Snapchat chat command from Beeper")
				}
			},
		}, nil
	}

	messageID := networkid.MessageID("matrix-" + stableID(transactionID))
	var replyTarget *localReplyTarget
	if msg.ReplyTo != nil {
		if isMediaPartID(string(msg.ReplyTo.PartID)) {
			return c.rejectMediaReply(msg, transactionID, mediaReplyRejectionNotice), nil
		}
		messages, err := c.getMessages(ctx, chatID)
		if err != nil {
			return nil, fmt.Errorf("could not load Snapchat messages to resolve reply target: %w", err)
		}
		if isMediaOnlyTarget(chatID, msg.ReplyTo.ID, messages) {
			return c.rejectMediaReply(msg, transactionID, mediaReplyRejectionNotice), nil
		}
		replyTarget = findLocalReplyTarget(chatID, msg.ReplyTo.ID, messages)
		if replyTarget == nil {
			return nil, fmt.Errorf(
				"could not find Snapchat message %q to reply to; sync the chat and try again",
				msg.ReplyTo.ID,
			)
		}
	}
	var result map[string]any
	err := postJSON(ctx, c.http, c.apiURL()+"/api/send", sendMessageRequest{
		ChatID:          chatID,
		Body:            body,
		BridgeMessageID: string(messageID),
		ReplyTo:         replyTarget,
	}, &result)
	if err != nil {
		return nil, fmt.Errorf("could not send Snapchat message: %w", err)
	}

	c.mu.Lock()
	key := echoKey(chatID, body)
	c.pending[key] = append(c.pending[key], pendingEcho{id: messageID, created: time.Now()})
	c.mu.Unlock()
	return &bridgev2.MatrixMessageResponse{
		DB: &database.Message{
			ID:        messageID,
			PartID:    "main",
			Room:      msg.Portal.PortalKey,
			SenderID:  localUserID,
			Timestamp: time.Now(),
		},
	}, nil
}

func (c *Client) rejectMediaReply(
	msg *bridgev2.MatrixMessage,
	transactionID string,
	notice string,
) *bridgev2.MatrixMessageResponse {
	chatID := string(msg.Portal.PortalKey.ID)
	rejectionID := networkid.MessageID("rejected-media-reply-" + stableID(chatID+"\x00"+transactionID))
	timestamp := time.Now()
	if msg.Event != nil && msg.Event.Timestamp != 0 {
		timestamp = time.UnixMilli(msg.Event.Timestamp)
	}
	return &bridgev2.MatrixMessageResponse{
		DB: &database.Message{
			ID:        rejectionID,
			PartID:    "main",
			Room:      msg.Portal.PortalKey,
			SenderID:  localUserID,
			Timestamp: timestamp,
		},
		PostSave: func(_ context.Context, saved *database.Message) {
			removal := c.login.QueueRemoteEvent(&simplevent.MessageRemove{
				EventMeta: simplevent.EventMeta{
					Type:      bridgev2.RemoteEventMessageRemove,
					PortalKey: c.portalKey(chatID),
					Sender:    messageEventSender("You", c.login.ID),
					Timestamp: saved.Timestamp,
				},
				TargetMessage:   saved.ID,
				HidePlaceholder: true,
			})
			if removal.Error != nil {
				c.connector.Bridge.Log.Err(removal.Error).
					Str("chat_id", chatID).
					Str("message_id", string(saved.ID)).
					Msg("Could not remove rejected Snapchat media reply from Beeper")
			} else if !removal.Success {
				c.connector.Bridge.Log.Error().
					Str("chat_id", chatID).
					Str("message_id", string(saved.ID)).
					Msg("Could not queue removal of rejected Snapchat media reply from Beeper")
			}
			c.queueTemporaryMediaRejectionNotice(chatID, transactionID, notice)
		},
	}
}

func (c *Client) handleMatrixImage(
	ctx context.Context,
	msg *bridgev2.MatrixMessage,
) (*bridgev2.MatrixMessageResponse, error) {
	if msg.ReplyTo != nil {
		return nil, fmt.Errorf("Snapchat Web does not support replying with an image attachment")
	}
	if msg.Content.GetCaption() != "" {
		return nil, fmt.Errorf("Snapchat Web image uploads do not support Matrix image captions")
	}
	if msg.Content.URL == "" && (msg.Content.File == nil || msg.Content.File.URL == "") {
		return nil, fmt.Errorf("Snapchat image message has no Matrix media URL")
	}
	chatID := string(msg.Portal.PortalKey.ID)
	transactionID := string(msg.InputTransactionID)
	if transactionID == "" && msg.Event != nil {
		transactionID = string(msg.Event.ID)
	}
	if transactionID == "" {
		transactionID = fmt.Sprintf("%s\x00image\x00%d", chatID, time.Now().UnixNano())
	}
	messageID := networkid.MessageID("matrix-" + stableID(transactionID))
	if !c.reserveOutgoingImage(chatID, string(messageID)) {
		return nil, fmt.Errorf(
			"Snapchat supports at most %d queued image uploads per chat",
			maxQueuedOutgoingImages,
		)
	}
	defer c.releaseOutgoingImage(chatID, string(messageID))

	unlockImageSend := c.lockOutgoingImage()
	defer unlockImageSend()

	mediaURL := msg.Content.URL
	if mediaURL == "" {
		mediaURL = msg.Content.File.URL
	}
	data, err := c.connector.Bridge.Bot.DownloadMedia(ctx, mediaURL, msg.Content.File)
	if err != nil {
		return nil, fmt.Errorf("could not download Matrix image: %w", err)
	}
	mimeType, err := validateOutgoingImage(data, msg.Content.Info)
	if err != nil {
		return nil, err
	}
	filename := outgoingImageFilename(msg.Content.GetFileName(), mimeType)

	query := url.Values{
		"chat_id":           {chatID},
		"filename":          {filename},
		"bridge_message_id": {string(messageID)},
	}
	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		c.apiURL()+"/api/send-image?"+query.Encode(),
		bytes.NewReader(data),
	)
	if err != nil {
		return nil, fmt.Errorf("could not create Snapchat image request: %w", err)
	}
	request.Header.Set("Content-Type", mimeType)
	response, err := c.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("could not send Snapchat image: %w", err)
	}
	defer response.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(response.Body, maxResponseLen+1))
	if err != nil {
		return nil, fmt.Errorf("could not read Snapchat image response: %w", err)
	}
	if len(responseBody) > maxResponseLen {
		return nil, fmt.Errorf("Snapchat image response exceeds the size limit")
	}
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return nil, fmt.Errorf(
			"could not send Snapchat image: local API returned %s: %s",
			response.Status,
			strings.TrimSpace(string(responseBody)),
		)
	}

	c.mu.Lock()
	key := echoKey(chatID, "Photo")
	c.pending[key] = append(c.pending[key], pendingEcho{id: messageID, created: time.Now()})
	c.mu.Unlock()
	return &bridgev2.MatrixMessageResponse{
		DB: &database.Message{
			ID:        messageID,
			PartID:    "main",
			Room:      msg.Portal.PortalKey,
			SenderID:  localUserID,
			Timestamp: time.Now(),
		},
	}, nil
}

func (c *Client) reserveOutgoingImage(chatID, messageID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.outgoingImageQueue == nil {
		c.outgoingImageQueue = make(map[string]map[string]struct{})
	}
	queue := c.outgoingImageQueue[chatID]
	if queue == nil {
		queue = make(map[string]struct{})
		c.outgoingImageQueue[chatID] = queue
	}
	if _, reserved := queue[messageID]; reserved {
		return true
	}
	if len(queue) >= maxQueuedOutgoingImages {
		return false
	}
	queue[messageID] = struct{}{}
	return true
}

func (c *Client) releaseOutgoingImage(chatID, messageID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.outgoingImageQueue[chatID], messageID)
}

func (c *Client) lockOutgoingImage() func() {
	c.outgoingImageMu.Lock()
	return c.outgoingImageMu.Unlock
}

func validateOutgoingImage(data []byte, info *event.FileInfo) (string, error) {
	if len(data) == 0 {
		return "", fmt.Errorf("Snapchat image attachment is empty")
	}
	if len(data) > maxOutgoingImageSize {
		return "", fmt.Errorf("Snapchat image attachment exceeds the 40 MiB limit")
	}
	detected := http.DetectContentType(data)
	switch detected {
	case "image/png", "image/jpeg", "image/gif":
	default:
		return "", fmt.Errorf("Snapchat Web supports PNG, JPEG, and GIF images only")
	}
	if info != nil {
		declared := strings.ToLower(strings.TrimSpace(info.MimeType))
		if declared != "" && declared != "application/octet-stream" &&
			declared != detected && !(declared == "image/jpg" && detected == "image/jpeg") {
			return "", fmt.Errorf(
				"Matrix image MIME type %q does not match its %s file data",
				declared,
				detected,
			)
		}
	}
	return detected, nil
}

func outgoingImageFilename(filename, mimeType string) string {
	filename = strings.TrimSpace(strings.ReplaceAll(filename, `\`, "/"))
	filename = path.Base(filename)
	if filename == "." || filename == "/" {
		filename = ""
	}
	extension := map[string]string{
		"image/png":  ".png",
		"image/jpeg": ".jpg",
		"image/gif":  ".gif",
	}[mimeType]
	if extension == "" {
		extension = ".png"
	}
	if filename == "" {
		return "image" + extension
	}
	currentExtension := strings.ToLower(path.Ext(filename))
	if currentExtension != extension && !(mimeType == "image/jpeg" && currentExtension == ".jpeg") {
		filename = strings.TrimSuffix(filename, path.Ext(filename)) + extension
	}
	return filename
}

func (c *Client) HandleMatrixMessageRemove(
	ctx context.Context,
	msg *bridgev2.MatrixMessageRemove,
) error {
	if msg.TargetMessage == nil {
		return fmt.Errorf("Snapchat message deletion has no target message")
	}
	chatID := string(msg.Portal.PortalKey.ID)
	eventKey := string(msg.TargetMessage.MXID)
	if msg.Event != nil {
		eventKey = string(msg.Event.ID)
	}
	if isMediaPartID(string(msg.TargetMessage.PartID)) {
		c.queueTemporaryNotice(
			chatID,
			"media-delete\x00"+eventKey,
			mediaDeletionNoticeContent(),
		)
		return nil
	}
	messages, err := c.getMessages(ctx, chatID)
	if err != nil {
		return fmt.Errorf("could not load Snapchat messages to resolve deletion target: %w", err)
	}
	if message := findLocalMessage(chatID, msg.TargetMessage.ID, messages); message != nil &&
		len(message.Media) > 0 {
		c.queueTemporaryNotice(
			chatID,
			"media-delete\x00"+eventKey,
			mediaDeletionNoticeContent(),
		)
		return nil
	}
	target := findLocalReplyTarget(chatID, msg.TargetMessage.ID, messages)
	if target == nil {
		return fmt.Errorf(
			"could not find Snapchat message %q to delete; sync the chat and try again",
			msg.TargetMessage.ID,
		)
	}
	var deletedMessage *timestampedLocalMessage
	for index := range messages {
		timestamp, err := parseTimestamp(messages[index].Timestamp)
		if err != nil {
			continue
		}
		if localMessageID(chatID, messages[index], timestamp) == msg.TargetMessage.ID {
			deletedMessage = &timestampedLocalMessage{
				Message:   messages[index],
				ID:        msg.TargetMessage.ID,
				Timestamp: timestamp,
			}
			break
		}
	}
	if deletedMessage == nil {
		return fmt.Errorf("could not find Snapchat message %q to delete", msg.TargetMessage.ID)
	}
	var result map[string]any
	err = postJSON(ctx, c.http, c.apiURL()+"/api/delete-message", deleteMessageRequest{
		ChatID: chatID,
		Target: target,
	}, &result)
	if err != nil {
		return fmt.Errorf("could not delete Snapchat message: %w", err)
	}

	for _, affected := range messageRemovalTargets(
		chatID, *deletedMessage, msg.TargetMessage.ID,
	) {
		queueResult := c.login.QueueRemoteEvent(&simplevent.MessageRemove{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventMessageRemove,
				PortalKey: c.portalKey(chatID),
				Sender:    messageEventSender(affected.Message.Author, c.login.ID),
				Timestamp: affected.Timestamp,
			},
			TargetMessage:   affected.ID,
			HidePlaceholder: true,
		})
		if queueResult.Error != nil {
			return fmt.Errorf(
				"could not remove detached media message %q from Beeper: %w",
				affected.ID,
				queueResult.Error,
			)
		} else if !queueResult.Success {
			return fmt.Errorf(
				"could not queue removal of detached media message %q from Beeper",
				affected.ID,
			)
		}
	}
	return nil
}

type timestampedLocalMessage struct {
	Message   localMessage
	ID        networkid.MessageID
	Timestamp time.Time
}

func messageRemovalTargets(
	chatID string,
	message timestampedLocalMessage,
	deletedMessageID networkid.MessageID,
) []timestampedLocalMessage {
	var targets []timestampedLocalMessage
	seen := make(map[networkid.MessageID]struct{})
	if message.Message.IsCustom {
		return nil
	}
	add := func(id networkid.MessageID) {
		if id == "" || id == deletedMessageID {
			return
		}
		if _, exists := seen[id]; exists {
			return
		}
		seen[id] = struct{}{}
		targets = append(targets, timestampedLocalMessage{
			Message:   message.Message,
			ID:        id,
			Timestamp: message.Timestamp,
		})
	}

	if len(message.Message.Media) == 0 {
		return nil
	}
	primaryBatchCount := (len(message.Message.Media) + maxBeeperMediaParts - 1) /
		maxBeeperMediaParts
	for batchIndex := 1; batchIndex < primaryBatchCount; batchIndex++ {
		add(networkid.MessageID(stableID(fmt.Sprintf(
			"%s\x00media-batch\x00%d", message.ID, batchIndex,
		))))
	}

	messageKey := stableMessageKey(
		chatID,
		message.Message.Author,
		strings.TrimSpace(message.Message.Body),
		message.Timestamp,
	)
	mediaID := networkid.MessageID(stableID(
		messageKey + "\x00media\x00" +
			mediaSeenKey(messageKey, message.Message.Media),
	))
	add(mediaID)
	followUpBatchCount := (len(message.Message.Media) + maxBeeperMediaParts - 1) /
		maxBeeperMediaParts
	for batchIndex := 1; batchIndex < followUpBatchCount; batchIndex++ {
		add(networkid.MessageID(stableID(fmt.Sprintf(
			"%s\x00media-batch\x00%d", mediaID, batchIndex,
		))))
	}
	return targets
}

func (c *Client) PreHandleMatrixReaction(
	_ context.Context,
	msg *bridgev2.MatrixReaction,
) (bridgev2.MatrixReactionPreResponse, error) {
	if msg.Content == nil ||
		msg.Content.RelatesTo.Type != event.RelAnnotation ||
		!isSnapchatReaction(msg.Content.RelatesTo.Key) {
		return bridgev2.MatrixReactionPreResponse{}, fmt.Errorf(
			"Snapchat supports only its eight standard emoji reactions",
		)
	}
	emoji, _ := normalizeSnapchatReaction(msg.Content.RelatesTo.Key)
	return bridgev2.MatrixReactionPreResponse{
		SenderID:     localUserID,
		EmojiID:      networkid.EmojiID(emoji),
		Emoji:        emoji,
		MaxReactions: 1,
	}, nil
}

func (c *Client) HandleMatrixReaction(
	ctx context.Context,
	msg *bridgev2.MatrixReaction,
) (*database.Reaction, error) {
	emoji, ok := normalizeSnapchatReaction(msg.Content.RelatesTo.Key)
	if !ok {
		return nil, fmt.Errorf("Snapchat supports only its eight standard emoji reactions")
	}
	if err := c.sendReaction(
		ctx,
		string(msg.Portal.PortalKey.ID),
		msg.TargetMessage.ID,
		string(msg.TargetMessage.PartID),
		emoji,
		false,
	); err != nil {
		if rejection, ok := err.(*mediaInteractionRejection); ok {
			c.rejectMediaReaction(ctx, msg, emoji, rejection.notice)
			partID := msg.TargetMessage.PartID
			if partID == "" {
				partID = "main"
			}
			reaction := &database.Reaction{
				Room:          msg.Portal.PortalKey,
				MessageID:     msg.TargetMessage.ID,
				MessagePartID: partID,
				SenderID:      localUserID,
				EmojiID:       networkid.EmojiID(emoji),
				Emoji:         emoji,
				Timestamp:     time.Now(),
			}
			if msg.Event != nil {
				reaction.MXID = msg.Event.ID
			}
			c.removeRejectedReactionFromDB(reaction)
			return reaction, nil
		}
		return nil, err
	}
	partID := msg.TargetMessage.PartID
	if partID == "" {
		partID = "main"
	}
	return &database.Reaction{
		Room:          msg.Portal.PortalKey,
		MessageID:     msg.TargetMessage.ID,
		MessagePartID: partID,
		SenderID:      localUserID,
		EmojiID:       networkid.EmojiID(emoji),
		Emoji:         emoji,
		Timestamp:     time.Now(),
	}, nil
}

func (c *Client) HandleMatrixReactionRemove(
	ctx context.Context,
	msg *bridgev2.MatrixReactionRemove,
) error {
	if msg.TargetReaction == nil {
		return fmt.Errorf("Snapchat reaction removal has no target reaction")
	}
	return c.sendReaction(
		ctx,
		string(msg.Portal.PortalKey.ID),
		msg.TargetReaction.MessageID,
		string(msg.TargetReaction.MessagePartID),
		msg.TargetReaction.Emoji,
		true,
	)
}

func (c *Client) sendReaction(
	ctx context.Context,
	chatID string,
	messageID networkid.MessageID,
	partID string,
	emoji string,
	remove bool,
) error {
	emoji, ok := normalizeSnapchatReaction(emoji)
	if !ok {
		return fmt.Errorf("Snapchat supports only its eight standard emoji reactions")
	}
	if isMediaPartID(partID) {
		return &mediaInteractionRejection{
			message: "reacting to Snapchat media attachments is not supported; react to a text message instead",
			notice:  mediaReactionRejectionNotice,
		}
	}
	messages, err := c.getMessages(ctx, chatID)
	if err != nil {
		return fmt.Errorf("could not load Snapchat messages to resolve reaction target: %w", err)
	}
	if isMediaOnlyTarget(chatID, messageID, messages) {
		return &mediaInteractionRejection{
			message: "reacting to Snapchat media-only messages is not supported; react to a text message instead",
			notice:  mediaReactionRejectionNotice,
		}
	}
	target := findLocalReplyTarget(chatID, messageID, messages)
	if target == nil {
		return fmt.Errorf(
			"could not find Snapchat message %q to react to; sync the chat and try again",
			messageID,
		)
	}
	var result map[string]any
	err = postJSON(ctx, c.http, c.apiURL()+"/api/react", sendReactionRequest{
		ChatID: chatID,
		Target: target,
		Emoji:  emoji,
		Remove: remove,
	}, &result)
	if err != nil {
		return fmt.Errorf("could not send Snapchat reaction: %w", err)
	}
	return nil
}

func (c *Client) rejectMediaReaction(
	ctx context.Context,
	msg *bridgev2.MatrixReaction,
	emoji string,
	notice string,
) {
	eventKey := ""
	if msg.Event != nil {
		eventKey = string(msg.Event.ID)
		if c.connector != nil && c.connector.Bridge != nil && c.connector.Bridge.Bot != nil {
			_, err := c.connector.Bridge.Bot.SendMessage(
				ctx,
				msg.Portal.MXID,
				event.EventRedaction,
				&event.Content{
					Parsed: &event.RedactionEventContent{Redacts: msg.Event.ID},
				},
				nil,
			)
			if err != nil {
				c.connector.Bridge.Log.Err(err).
					Str("event_id", string(msg.Event.ID)).
					Msg("Could not remove rejected Snapchat media reaction from Beeper")
			}
		} else if c.connector != nil && c.connector.Bridge != nil {
			c.connector.Bridge.Log.Error().
				Str("event_id", string(msg.Event.ID)).
				Msg("Could not remove rejected Snapchat media reaction from Beeper: Matrix bot is unavailable")
		}
	}
	if eventKey == "" {
		eventKey = fmt.Sprintf("%s\x00%s\x00%d", msg.TargetMessage.ID, emoji, time.Now().UnixNano())
	}
	c.queueTemporaryMediaRejectionNotice(
		string(msg.Portal.PortalKey.ID),
		"reaction\x00"+eventKey,
		notice,
	)
}

func (c *Client) removeRejectedReactionFromDB(reaction *database.Reaction) {
	if c.connector == nil || c.connector.Bridge == nil || c.connector.Bridge.DB == nil {
		return
	}
	go func() {
		baseCtx := c.connector.Bridge.BackgroundCtx
		if baseCtx == nil {
			baseCtx = context.Background()
		}
		ctx, cancel := context.WithTimeout(baseCtx, 10*time.Second)
		defer cancel()
		ticker := time.NewTicker(25 * time.Millisecond)
		defer ticker.Stop()
		for {
			stored, err := c.connector.Bridge.DB.Reaction.GetByID(
				ctx,
				reaction.Room.Receiver,
				reaction.MessageID,
				reaction.MessagePartID,
				reaction.SenderID,
				reaction.EmojiID,
			)
			if err != nil {
				c.connector.Bridge.Log.Err(err).
					Str("message_id", string(reaction.MessageID)).
					Msg("Could not find rejected Snapchat media reaction in Beeper database for cleanup")
				return
			}
			if stored != nil {
				if err = c.connector.Bridge.DB.Reaction.Delete(ctx, stored); err != nil {
					c.connector.Bridge.Log.Err(err).
						Str("message_id", string(reaction.MessageID)).
						Msg("Could not remove rejected Snapchat media reaction from Beeper database")
				}
				return
			}
			select {
			case <-ctx.Done():
				c.connector.Bridge.Log.Error().
					Str("message_id", string(reaction.MessageID)).
					Msg("Timed out waiting to clean up rejected Snapchat media reaction from Beeper database")
				return
			case <-ticker.C:
			}
		}
	}()
}

func (c *Client) queueTemporaryMediaRejectionNotice(chatID, eventKey, notice string) {
	c.queueTemporaryNotice(chatID, eventKey, mediaRejectionNoticeContent(notice))
}

func (c *Client) queueTemporaryNotice(
	chatID string,
	eventKey string,
	content *event.MessageEventContent,
) {
	if c.login == nil || c.connector == nil || c.connector.Bridge == nil {
		return
	}
	messageID := networkid.MessageID("media-rejection-notice-" + stableID(chatID+"\x00"+eventKey))
	now := time.Now()
	result := c.login.QueueRemoteEvent(&simplevent.PreConvertedMessage{
		EventMeta: simplevent.EventMeta{
			Type:      bridgev2.RemoteEventMessage,
			PortalKey: c.portalKey(chatID),
			Sender:    messageEventSender("You", c.login.ID),
			Timestamp: now,
			PostHandleFunc: func(context.Context, *bridgev2.Portal) {
				time.AfterFunc(5*time.Second, func() {
					removal := c.login.QueueRemoteEvent(&simplevent.MessageRemove{
						EventMeta: simplevent.EventMeta{
							Type:      bridgev2.RemoteEventMessageRemove,
							PortalKey: c.portalKey(chatID),
							Sender:    messageEventSender("You", c.login.ID),
							Timestamp: time.Now(),
						},
						TargetMessage:   messageID,
						HidePlaceholder: true,
					})
					if removal.Error != nil {
						c.connector.Bridge.Log.Err(removal.Error).
							Str("message_id", string(messageID)).
							Msg("Could not remove temporary Snapchat media rejection notice from Beeper")
					} else if !removal.Success {
						c.connector.Bridge.Log.Error().
							Str("message_id", string(messageID)).
							Msg("Could not queue removal of temporary Snapchat media rejection notice from Beeper")
					}
				})
			},
		},
		ID: messageID,
		Data: &bridgev2.ConvertedMessage{
			Parts: []*bridgev2.ConvertedMessagePart{{
				ID:      "main",
				Type:    event.EventMessage,
				Content: content,
			}},
		},
	})
	if result.Error != nil {
		c.connector.Bridge.Log.Err(result.Error).
			Str("message_id", string(messageID)).
			Msg("Could not queue Snapchat media rejection notice")
	} else if !result.Success {
		c.connector.Bridge.Log.Error().
			Str("message_id", string(messageID)).
			Msg("Could not queue Snapchat media rejection notice")
	}
}

func mediaRejectionNoticeContent(notice string) *event.MessageEventContent {
	return &event.MessageEventContent{
		MsgType:       event.MsgText,
		Body:          notice,
		Format:        event.FormatHTML,
		FormattedBody: "<em>" + html.EscapeString(notice) + "</em>",
	}
}

func mediaDeletionNoticeContent() *event.MessageEventContent {
	return &event.MessageEventContent{
		MsgType: event.MsgText,
		Body:    mediaDeletionNotice,
		Format:  event.FormatHTML,
		FormattedBody: "Deleting a Snapchat media attachment is not supported; " +
			`<a href="https://web.snapchat.com">Open Snapchat instead</a>`,
	}
}

func isSnapchatReaction(emoji string) bool {
	_, ok := normalizeSnapchatReaction(emoji)
	return ok
}

func normalizeSnapchatReaction(emoji string) (string, bool) {
	normalized := strings.ReplaceAll(strings.ReplaceAll(emoji, "\ufe0e", ""), "\ufe0f", "")
	for _, allowed := range snapchatReactions {
		canonical := strings.ReplaceAll(strings.ReplaceAll(allowed, "\ufe0e", ""), "\ufe0f", "")
		if normalized == canonical {
			return allowed, true
		}
	}
	return "", false
}

func (c *Client) syncLoop(ctx context.Context) {
	interval, err := time.ParseDuration(c.connector.Config.PollInterval)
	if err != nil {
		c.reportState(status.StateUnknownError, err.Error())
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		if err := c.syncOnce(ctx); err != nil {
			c.mu.Lock()
			logError := c.lastSyncError != err.Error()
			c.lastSyncError = err.Error()
			c.mu.Unlock()
			if logError {
				c.connector.Bridge.Log.Err(err).Msg("Snapchat bridge sync failed")
			}
			c.setOnline(false)
			c.reportState(status.StateTransientDisconnect, err.Error())
		} else {
			c.mu.Lock()
			c.lastSyncError = ""
			c.mu.Unlock()
			c.setOnline(true)
			c.reportState(status.StateConnected, "")
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (c *Client) syncOnce(ctx context.Context) error {
	c.prunePending(time.Now())
	var state localStatus
	if err := getJSON(ctx, c.http, c.apiURL()+"/api/status", &state); err != nil {
		return fmt.Errorf("local Snapchat app is unavailable: %w", err)
	}
	if !state.LoggedIn {
		return fmt.Errorf("local Snapchat app is not signed in")
	}
	chats, err := c.getChats(ctx)
	if err != nil {
		return err
	}
	typingChats := make(map[string]struct{}, len(state.TypingChats))
	for _, chatID := range state.TypingChats {
		typingChats[chatID] = struct{}{}
	}
	openedChats := make(map[string]struct{}, len(state.OpenedChats))
	for _, chatID := range state.OpenedChats {
		openedChats[chatID] = struct{}{}
	}
	enabledChats := make(map[string]bool, len(chats))
	for _, chat := range chats {
		enabledChats[chat.ID] = bool(chat.Enabled)
	}
	c.mu.Lock()
	inactiveTypingChats := make([]string, 0)
	for chatID := range c.typing {
		_, typing := typingChats[chatID]
		if !typing || !enabledChats[chatID] {
			inactiveTypingChats = append(inactiveTypingChats, chatID)
		}
	}
	c.mu.Unlock()
	for _, chatID := range inactiveTypingChats {
		if err := c.syncRemoteTyping(ctx, chatID, false, bridgev2.EventSender{}); err != nil {
			return err
		}
	}
	c.mu.Lock()
	refreshInfoPending := c.refreshInfoPending
	c.mu.Unlock()
	for _, chat := range chats {
		if !bool(chat.Enabled) && !c.shouldRefreshPortal(chat.ID) {
			continue
		}
		key := c.portalKey(chat.ID)
		portal, err := c.connector.Bridge.GetPortalByKey(ctx, key)
		if err != nil {
			return fmt.Errorf("could not load portal for %q: %w", chat.Title, err)
		}
		messages, err := c.getMessages(ctx, chat.ID)
		if err != nil {
			return fmt.Errorf("could not read messages for %q: %w", chat.Title, err)
		}
		info := buildChatInfoForChat(chat, messages, c.login.ID)
		if portal.MXID == "" {
			if err := portal.CreateMatrixRoom(ctx, c.login, info); err != nil {
				return fmt.Errorf("could not create Beeper chat for %q: %w", chat.Title, err)
			}
		} else {
			shouldUpdate := shouldUpdateChatInfo(info, portal)
			if refreshInfoPending || c.shouldRefreshPortal(chat.ID) || shouldUpdate {
				portal.UpdateInfo(ctx, info, c.login, nil, time.Now())
			}
		}
		c.clearPortalRefresh(chat.ID)
		if _, typing := typingChats[chat.ID]; typing && bool(chat.Enabled) {
			if err := c.syncRemoteTyping(
				ctx,
				chat.ID,
				true,
				remoteTypingSender(chat, messages, c.login.ID),
			); err != nil {
				return err
			}
		}
		for _, message := range messages {
			if err := c.queueMessage(ctx, chat.ID, message, messages); err != nil {
				return err
			}
		}
		if bool(chat.Enabled) {
			if _, opened := openedChats[chat.ID]; opened {
				if err := c.queueOpenedReceipt(chat.ID, chat, messages); err != nil {
					return err
				}
			}
		}
		if err := c.syncCustomMessageRemovals(
			ctx, chat.ID, chat.Title, messages,
		); err != nil {
			return err
		}
	}
	if refreshInfoPending {
		c.mu.Lock()
		c.refreshInfoPending = false
		c.mu.Unlock()
	}
	return nil
}

func (c *Client) syncRemoteTyping(
	ctx context.Context,
	chatID string,
	typing bool,
	sender bridgev2.EventSender,
) error {
	c.mu.Lock()
	if c.typing == nil {
		c.typing = make(map[string]remoteTypingState)
	}
	previous, wasTyping := c.typing[chatID]
	if !typing && !wasTyping {
		c.mu.Unlock()
		return nil
	}
	if !typing {
		sender = previous.sender
	}
	c.mu.Unlock()

	timeout := time.Duration(0)
	if typing {
		interval, err := time.ParseDuration(c.connector.Config.PollInterval)
		if err != nil {
			return fmt.Errorf("could not parse Beeper typing refresh interval: %w", err)
		}
		timeout = 2 * interval
	}
	result := c.login.QueueRemoteEvent(&simplevent.Typing{
		EventMeta: simplevent.EventMeta{
			Type:      bridgev2.RemoteEventTyping,
			PortalKey: c.portalKey(chatID),
			Sender:    sender,
			Timestamp: time.Now(),
		},
		Timeout: timeout,
		Type:    bridgev2.TypingTypeText,
	})
	if result.Error != nil {
		return fmt.Errorf("could not sync Snapchat typing status for chat %q: %w", chatID, result.Error)
	}
	if result.Ignored {
		if typing {
			c.mu.Lock()
			c.typing[chatID] = remoteTypingState{sender: sender}
			c.mu.Unlock()
		} else {
			c.mu.Lock()
			delete(c.typing, chatID)
			c.mu.Unlock()
		}
		if (typing && (!wasTyping || previous.delivered)) || (!typing && wasTyping) {
			c.connector.Bridge.Log.Warn().
				Str("chat_id", chatID).
				Bool("typing", typing).
				Msg("Beeper ignored Snapchat typing event; no typing indicator was delivered")
		}
		return nil
	}

	c.mu.Lock()
	if typing {
		c.typing[chatID] = remoteTypingState{sender: sender, delivered: true}
	} else {
		delete(c.typing, chatID)
	}
	c.mu.Unlock()
	if (typing && (!wasTyping || !previous.delivered)) || (!typing && wasTyping) {
		c.connector.Bridge.Log.Info().
			Str("chat_id", chatID).
			Str("sender", string(sender.Sender)).
			Bool("typing", typing).
			Int64("timeout_ms", timeout.Milliseconds()).
			Msg("Queued Snapchat typing indicator for Beeper")
	}
	return nil
}

func remoteTypingSender(
	chat localChat,
	messages []localMessage,
	loginID networkid.UserLoginID,
) bridgev2.EventSender {
	for _, message := range messages {
		author := strings.TrimSpace(message.Author)
		if message.IsCustom || author == "" ||
			strings.EqualFold(author, "you") || strings.EqualFold(author, "ik") {
			continue
		}
		return messageEventSender(author, loginID)
	}
	return messageEventSender(chat.Title, loginID)
}

func shouldUpdateChatInfo(info *bridgev2.ChatInfo, portal *bridgev2.Portal) bool {
	if info.Type == nil || info.Members == nil {
		return false
	}
	if portal.RoomType == database.RoomTypeGroupDM &&
		*info.Type == database.RoomTypeDM {
		*info.Type = database.RoomTypeGroupDM
		info.Members.OtherUserID = ""
		info.Members.IsFull = false
		info.Avatar = nil
	}
	if info.Type != nil && *info.Type == database.RoomTypeGroupDM &&
		portal.AvatarSet && portal.AvatarMXC != "" {
		info.Avatar = &bridgev2.Avatar{
			ID:     networkid.AvatarID("group-avatar-removed"),
			Remove: true,
		}
		info.ExcludeChangesFromTimeline = true
	}
	nameChanged := !portal.NameIsCustom &&
		info.Name != nil && portal.Name != *info.Name
	avatarChanged := info.Avatar != nil &&
		(info.Avatar.ID != portal.AvatarID ||
			(!portal.AvatarSet && portal.AvatarMXC == ""))
	if avatarChanged {
		info.ExcludeChangesFromTimeline = true
	}
	return nameChanged ||
		portal.RoomType != *info.Type ||
		portal.OtherUserID != info.Members.OtherUserID ||
		avatarChanged
}

func (c *Client) shouldRefreshPortal(chatID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.refreshPortals[chatID]
	return ok
}

func (c *Client) clearPortalRefresh(chatID string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.refreshPortals, chatID)
}

func (c *Client) syncCustomMessageRemovals(
	ctx context.Context,
	chatID string,
	title string,
	history []localMessage,
) error {
	endpoint := c.apiURL() + "/api/message-removals?chat=" + url.QueryEscape(chatID)
	var response localMessageRemovalsResponse
	if err := getJSON(ctx, c.http, endpoint, &response); err != nil {
		return fmt.Errorf("could not read withdrawn Snapchat notifications: %w", err)
	}
	for _, removal := range response.Removals {
		timestamp, err := parseTimestamp(removal.Timestamp)
		if err != nil {
			return fmt.Errorf(
				"invalid withdrawn Snapchat notification timestamp %q: %w",
				removal.Timestamp, err,
			)
		}
		key := removal.ChatID + "\x00" + removal.CustomID
		c.mu.Lock()
		if c.queuedRemovals == nil {
			c.queuedRemovals = make(map[string]struct{})
		}
		if _, queued := c.queuedRemovals[key]; queued {
			c.mu.Unlock()
			continue
		}
		c.queuedRemovals[key] = struct{}{}
		c.mu.Unlock()
		custom := localMessage{
			Author:       "You",
			CustomSender: title,
			IsCustom:     true,
		}
		result := c.login.QueueRemoteEvent(&simplevent.MessageRemove{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventMessageRemove,
				PortalKey: c.portalKey(chatID),
				Sender: messageEventSender(
					messageSenderName(custom, history), c.login.ID,
				),
				Timestamp: timestamp,
				PostHandleFunc: func(handleCtx context.Context, _ *bridgev2.Portal) {
					defer func() {
						c.mu.Lock()
						delete(c.queuedRemovals, key)
						c.mu.Unlock()
					}()
					if err := c.acknowledgeCustomMessageRemoval(
						handleCtx, removal.ChatID, removal.CustomID,
					); err != nil {
						c.connector.Bridge.Log.Err(err).
							Str("chat_id", removal.ChatID).
							Str("custom_id", removal.CustomID).
							Msg("Could not acknowledge withdrawn Snapchat notification")
					}
				},
			},
			TargetMessage:   networkid.MessageID(removal.CustomID),
			HidePlaceholder: true,
		})
		if result.Error != nil {
			c.mu.Lock()
			delete(c.queuedRemovals, key)
			c.mu.Unlock()
			return fmt.Errorf(
				"could not queue removal of Snapchat notification %q: %w",
				removal.CustomID, result.Error,
			)
		}
		if !result.Success {
			c.mu.Lock()
			delete(c.queuedRemovals, key)
			c.mu.Unlock()
			return fmt.Errorf(
				"could not queue removal of Snapchat notification %q",
				removal.CustomID,
			)
		}
	}
	return nil
}

func (c *Client) acknowledgeCustomMessageRemoval(
	ctx context.Context,
	chatID string,
	customID string,
) error {
	return postJSON(ctx, c.http, c.apiURL()+"/api/message-removals/ack", map[string]string{
		"chat_id":   chatID,
		"custom_id": customID,
	}, nil)
}

func buildChatInfo(title string, messages []localMessage, loginID networkid.UserLoginID) *bridgev2.ChatInfo {
	return buildChatInfoWithAvatar(title, messages, loginID, "")
}

func buildChatInfoWithAvatar(
	title string,
	messages []localMessage,
	loginID networkid.UserLoginID,
	avatarURL string,
) *bridgev2.ChatInfo {
	return buildChatInfoWithAvatarVersion(title, messages, loginID, avatarURL, "")
}

func buildChatInfoWithAvatarVersion(
	title string,
	messages []localMessage,
	loginID networkid.UserLoginID,
	avatarURL string,
	avatarVersion string,
) *bridgev2.ChatInfo {
	return buildChatInfoWithDetails(
		title, messages, loginID, avatarURL, avatarVersion, false,
	)
}

func buildChatInfoForChat(
	chat localChat,
	messages []localMessage,
	loginID networkid.UserLoginID,
) *bridgev2.ChatInfo {
	return buildChatInfoWithDetails(
		chat.Title, messages, loginID, chat.AvatarURL, chat.AvatarVersion,
		bool(chat.IsGroup),
	)
}

func buildChatInfoWithDetails(
	title string,
	messages []localMessage,
	loginID networkid.UserLoginID,
	avatarURL string,
	avatarVersion string,
	isGroup bool,
) *bridgev2.ChatInfo {
	recipients := make(map[string]string)
	for _, message := range messages {
		if message.IsCustom {
			continue
		}
		author := strings.Join(strings.Fields(message.Author), " ")
		if author == "" || strings.EqualFold(author, "you") || strings.EqualFold(author, "ik") {
			continue
		}
		key := strings.ToLower(author)
		if _, exists := recipients[key]; !exists {
			recipients[key] = author
		}
	}

	roomType := database.RoomTypeDM
	if len(recipients) > 1 {
		roomType = database.RoomTypeGroupDM
	}
	if isGroup {
		roomType = database.RoomTypeGroupDM
	}
	if len(recipients) == 0 && strings.TrimSpace(title) == "" {
		roomType = database.RoomTypeGroupDM
	}

	members := &bridgev2.ChatMemberList{
		IsFull:           roomType == database.RoomTypeDM,
		TotalMemberCount: len(recipients) + 1,
		MemberMap:        make(bridgev2.ChatMemberMap, len(recipients)+1),
	}
	members.MemberMap[localUserID] = bridgev2.ChatMember{
		EventSender: bridgev2.EventSender{IsFromMe: true, SenderLogin: loginID, Sender: localUserID},
		Membership:  event.MembershipJoin,
	}
	for _, recipient := range recipients {
		userID := makeUserID(recipient)
		members.MemberMap[userID] = bridgev2.ChatMember{
			EventSender: bridgev2.EventSender{Sender: userID},
			Membership:  event.MembershipJoin,
			UserInfo:    &bridgev2.UserInfo{Name: stringPointer(recipient)},
		}
	}

	if roomType == database.RoomTypeDM {
		recipient := title
		for _, author := range recipients {
			recipient = author
		}
		if recipient != "" {
			userID := makeUserID(recipient)
			members.OtherUserID = userID
			if _, ok := members.MemberMap[userID]; !ok {
				members.MemberMap[userID] = bridgev2.ChatMember{
					EventSender: bridgev2.EventSender{Sender: userID},
					Membership:  event.MembershipJoin,
					UserInfo:    &bridgev2.UserInfo{Name: stringPointer(recipient)},
				}
				members.TotalMemberCount = 2
			}
		}
	}

	info := &bridgev2.ChatInfo{
		Name:    stringPointer(title),
		Type:    &roomType,
		Members: members,
	}
	if roomType == database.RoomTypeDM && !isGroup && isBitmojiAvatarURL(avatarURL) {
		hash := sha256.Sum256([]byte(avatarURL + "\x00" + avatarVersion))
		info.Avatar = &bridgev2.Avatar{
			ID: networkid.AvatarID(hex.EncodeToString(hash[:])),
			Get: func(ctx context.Context) ([]byte, error) {
				return downloadBitmojiAvatar(ctx, avatarURL)
			},
		}
	}
	return info
}

func isBitmojiAvatarURL(rawURL string) bool {
	parsed, err := url.Parse(rawURL)
	return err == nil &&
		parsed.Scheme == "https" &&
		strings.EqualFold(parsed.Hostname(), "cf-st.sc-cdn.net") &&
		parsed.Port() == "" &&
		parsed.User == nil &&
		strings.HasPrefix(parsed.Path, "/3d/render/")
}

func downloadBitmojiAvatar(ctx context.Context, avatarURL string) ([]byte, error) {
	if !isBitmojiAvatarURL(avatarURL) {
		return nil, fmt.Errorf("invalid Snapchat Bitmoji avatar URL")
	}
	client := &http.Client{
		Timeout: 15 * time.Second,
		CheckRedirect: func(request *http.Request, _ []*http.Request) error {
			if !isBitmojiAvatarURL(request.URL.String()) {
				return fmt.Errorf("Snapchat Bitmoji avatar redirected to an untrusted URL")
			}
			return nil
		},
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, avatarURL, nil)
	if err != nil {
		return nil, fmt.Errorf("could not create Snapchat Bitmoji avatar request: %w", err)
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("could not download Snapchat Bitmoji avatar: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("could not download Snapchat Bitmoji avatar: %s", response.Status)
	}
	if !strings.HasPrefix(strings.ToLower(response.Header.Get("Content-Type")), "image/") {
		return nil, fmt.Errorf("Snapchat Bitmoji avatar response is not an image")
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxAvatarSize+1))
	if err != nil {
		return nil, fmt.Errorf("could not read Snapchat Bitmoji avatar: %w", err)
	}
	if len(data) > maxAvatarSize {
		return nil, fmt.Errorf("Snapchat Bitmoji avatar exceeds the 10 MiB limit")
	}
	return data, nil
}

func stringPointer(value string) *string {
	return &value
}

func (c *Client) queueMessage(
	ctx context.Context,
	chatID string,
	message localMessage,
	history []localMessage,
) error {
	body := strings.TrimSpace(message.Body)
	if (body == "" && len(message.Media) == 0) || message.Status == "sending" {
		return nil
	}
	timestamp, err := parseTimestamp(message.Timestamp)
	if err != nil {
		return fmt.Errorf("invalid local message timestamp %q: %w", message.Timestamp, err)
	}
	key := stableMessageKey(chatID, message.Author, body, timestamp)
	c.mu.Lock()
	_, forceReplay := c.replayMessages[key]
	delete(c.replayMessages, key)
	if _, ok := c.seen[key]; ok && !forceReplay {
		c.mu.Unlock()
		if len(message.Media) > 0 {
			if err := c.queueMediaFollowUp(ctx, chatID, message, history, timestamp, key); err != nil {
				return err
			}
		}
		return c.queueReaction(
			chatID, message, timestamp, localMessageID(chatID, message, timestamp), history,
		)
	}
	messageID := localMessageID(chatID, message, timestamp)
	fromMe := normalizeAuthor(message.Author) == "You"
	if message.BridgeMessageID != "" {
		messageID = networkid.MessageID(message.BridgeMessageID)
	}
	var matchedEcho string
	pendingIndex := -1
	if fromMe {
		matchedEcho = echoKey(chatID, body)
		closestDelta := 2 * time.Minute
		for index, pending := range c.pending[matchedEcho] {
			delta := timestamp.Sub(pending.created)
			if delta < -2*time.Minute || delta > 2*time.Minute {
				continue
			}
			if delta < 0 {
				delta = -delta
			}
			if delta <= closestDelta {
				closestDelta = delta
				pendingIndex = index
			}
		}
		if pendingIndex >= 0 {
			messageID = c.pending[matchedEcho][pendingIndex].id
		} else {
			matchedEcho = ""
		}
	}
	if shouldSkipExistingBridgeMessage(message, pendingIndex, forceReplay) {
		c.seen[key] = struct{}{}
		c.mu.Unlock()
		return c.queueReaction(chatID, message, timestamp, messageID, history)
	}

	c.mu.Unlock()

	sender := messageEventSender(messageSenderName(message, history), c.login.ID)
	parts := make([]*bridgev2.ConvertedMessagePart, 0, len(message.Media)+1)
	if body != "" && !message.MediaOnly {
		content := localMessageContent(message, body)
		parts = append(parts, &bridgev2.ConvertedMessagePart{
			ID:      "main",
			Type:    event.EventMessage,
			Content: content,
		})
	}
	if len(message.Media) > 0 {
		mediaParts, err := c.uploadMediaParts(ctx, chatID, message.Media)
		if err != nil {
			return err
		}
		parts = append(parts, mediaParts...)
	}
	partBatches := splitConvertedMessageParts(parts, maxBeeperMediaParts)
	for index, batch := range partBatches {
		batchID := messageID
		if index > 0 {
			batchID = networkid.MessageID(stableID(
				fmt.Sprintf("%s\x00media-batch\x00%d", messageID, index),
			))
		}
		result := c.login.QueueRemoteEvent(&simplevent.PreConvertedMessage{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventMessage,
				PortalKey: c.portalKey(chatID),
				Sender:    sender,
				Timestamp: timestamp.Add(time.Duration(index) * time.Millisecond),
			},
			ID: batchID,
			Data: &bridgev2.ConvertedMessage{
				ReplyTo: findReplyTarget(chatID, message, history),
				Parts:   batch,
			},
		})
		if result.Error != nil {
			c.mu.Lock()
			if forceReplay || len(partBatches) > 1 {
				if c.replayMessages == nil {
					c.replayMessages = make(map[string]struct{})
				}
				c.replayMessages[key] = struct{}{}
			}
			c.mu.Unlock()
			return fmt.Errorf("could not queue Snapchat message %q: %w", batchID, result.Error)
		}
		if result.Ignored && !(fromMe && pendingIndex >= 0 && index == 0) &&
			len(partBatches) == 1 {
			return fmt.Errorf("bridge ignored Snapchat message %q", batchID)
		}
	}
	if len(message.Media) > 0 {
		c.connector.Bridge.Log.Info().
			Str("chat_id", chatID).
			Str("message_id", string(messageID)).
			Int("attachments", len(message.Media)).
			Int("batches", len(partBatches)).
			Msg("Queued Snapchat message with media for Beeper")
	}
	c.mu.Lock()
	c.seen[key] = struct{}{}
	if len(message.Media) > 0 {
		if c.seenMedia == nil {
			c.seenMedia = make(map[string]struct{})
		}
		c.seenMedia[mediaSeenKey(key, message.Media)] = struct{}{}
	}
	if pendingIndex >= 0 {
		pending := c.pending[matchedEcho]
		c.pending[matchedEcho] = append(pending[:pendingIndex], pending[pendingIndex+1:]...)
		if len(c.pending[matchedEcho]) == 0 {
			delete(c.pending, matchedEcho)
		}
	}
	c.mu.Unlock()
	return c.queueReaction(chatID, message, timestamp, messageID, history)
}

func (c *Client) queueOpenedReceipt(
	chatID string,
	chat localChat,
	messages []localMessage,
) error {
	target, targetTimestamp, found, err := latestChatMessage(chat.Title, messages)
	if err != nil {
		return err
	}
	if !found {
		c.connector.Bridge.Log.Debug().
			Str("chat_id", chatID).
			Msg("Snapchat reported an opened chat, but there is no message to receipt")
		return nil
	}

	targetID := localMessageID(chatID, target, targetTimestamp)
	c.mu.Lock()
	if previousID, ok := c.seenReadReceipts[chatID]; ok && previousID == targetID {
		c.mu.Unlock()
		c.connector.Bridge.Log.Debug().
			Str("chat_id", chatID).
			Str("message_id", string(targetID)).
			Msg("Skipping duplicate Snapchat opened receipt")
		return nil
	}
	c.mu.Unlock()

	result := c.login.QueueRemoteEvent(&simplevent.Receipt{
		EventMeta: simplevent.EventMeta{
			Type:      bridgev2.RemoteEventReadReceipt,
			PortalKey: c.portalKey(chatID),
			Sender:    remoteTypingSender(chat, messages, c.login.ID),
			Timestamp: time.Now(),
		},
		LastTarget: targetID,
		ReadUpTo:   targetTimestamp,
	})
	if result.Error != nil {
		c.connector.Bridge.Log.Err(result.Error).
			Str("chat_id", chatID).
			Str("message_id", string(targetID)).
			Msg("Failed to queue Snapchat opened receipt for Beeper")
		return fmt.Errorf("could not queue Snapchat opened receipt for %q: %w", targetID, result.Error)
	}
	if result.Ignored {
		c.connector.Bridge.Log.Warn().
			Str("chat_id", chatID).
			Str("message_id", string(targetID)).
			Msg("Beeper bridge ignored Snapchat opened receipt")
		return fmt.Errorf("bridge ignored Snapchat opened receipt for %q", targetID)
	}
	c.mu.Lock()
	if c.seenReadReceipts == nil {
		c.seenReadReceipts = make(map[string]networkid.MessageID)
	}
	c.seenReadReceipts[chatID] = targetID
	c.mu.Unlock()
	c.connector.Bridge.Log.Info().
		Str("chat_id", chatID).
		Str("message_id", string(targetID)).
		Time("read_up_to", targetTimestamp).
		Msg("Queued Snapchat opened receipt for Beeper")
	return nil
}

func latestChatMessage(
	chatTitle string,
	messages []localMessage,
) (localMessage, time.Time, bool, error) {
	var target localMessage
	var targetTimestamp time.Time
	found := false
	for _, message := range messages {
		timestamp, err := parseTimestamp(message.Timestamp)
		if err != nil {
			return localMessage{}, time.Time{}, false, fmt.Errorf(
				"invalid message timestamp %q in opened chat %q: %w",
				message.Timestamp, chatTitle, err,
			)
		}
		if !found || timestamp.After(targetTimestamp) {
			target = message
			targetTimestamp = timestamp
			found = true
		}
	}
	return target, targetTimestamp, found, nil
}

func localMessageContent(
	message localMessage,
	body string,
) *event.MessageEventContent {
	content := &event.MessageEventContent{
		MsgType: event.MsgText,
		Body:    body,
	}
	if message.LinkURL == "" {
		return content
	}
	link, err := url.ParseRequestURI(message.LinkURL)
	if err != nil || link.Scheme != "https" ||
		link.Host != "www.snapchat.com" || link.User != nil {
		return content
	}
	content.Body = body
	content.Format = event.FormatHTML
	content.FormattedBody = "<a href=\"" + html.EscapeString(link.String()) +
		"\" style=\"color:inherit;text-decoration:none\"><strong>" +
		html.EscapeString(body) + "</strong></a>"
	return content
}

func (c *Client) queueMediaFollowUp(
	ctx context.Context,
	chatID string,
	message localMessage,
	history []localMessage,
	timestamp time.Time,
	messageKey string,
) error {
	fingerprint := mediaSeenKey(messageKey, message.Media)
	c.mu.Lock()
	if _, alreadySeen := c.seenMedia[fingerprint]; alreadySeen {
		c.mu.Unlock()
		return nil
	}
	c.mu.Unlock()

	parts, err := c.uploadMediaParts(ctx, chatID, message.Media)
	if err != nil {
		return err
	}
	mediaID := networkid.MessageID(stableID(messageKey + "\x00media\x00" + fingerprint))
	partBatches := splitConvertedMessageParts(parts, maxBeeperMediaParts)
	for index, batch := range partBatches {
		batchID := mediaID
		if index > 0 {
			batchID = networkid.MessageID(stableID(
				fmt.Sprintf("%s\x00media-batch\x00%d", mediaID, index),
			))
		}
		result := c.login.QueueRemoteEvent(&simplevent.PreConvertedMessage{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventMessage,
				PortalKey: c.portalKey(chatID),
				Sender:    messageEventSender(messageSenderName(message, history), c.login.ID),
				Timestamp: timestamp.Add(time.Duration(index) * time.Millisecond),
			},
			ID: batchID,
			Data: &bridgev2.ConvertedMessage{
				ReplyTo: findReplyTarget(chatID, message, history),
				Parts:   batch,
			},
		})
		if result.Error != nil {
			return fmt.Errorf("could not queue Snapchat media %q: %w", batchID, result.Error)
		}
	}
	c.connector.Bridge.Log.Info().
		Str("chat_id", chatID).
		Str("message_id", string(mediaID)).
		Int("attachments", len(parts)).
		Int("batches", len(partBatches)).
		Msg("Queued Snapchat media follow-up for Beeper")
	c.mu.Lock()
	if c.seenMedia == nil {
		c.seenMedia = make(map[string]struct{})
	}
	c.seenMedia[fingerprint] = struct{}{}
	c.mu.Unlock()
	return nil
}

func (c *Client) uploadMediaParts(
	ctx context.Context,
	chatID string,
	media []localMedia,
) ([]*bridgev2.ConvertedMessagePart, error) {
	if len(media) > maxBeeperMediaParts {
		return nil, fmt.Errorf(
			"Snapchat message has %d media files; Beeper supports at most %d per message",
			len(media), maxBeeperMediaParts,
		)
	}
	portal, err := c.connector.Bridge.GetPortalByKey(ctx, c.portalKey(chatID))
	if err != nil {
		return nil, fmt.Errorf("could not load portal to upload Snapchat media: %w", err)
	}
	if portal.MXID == "" {
		return nil, fmt.Errorf("could not upload Snapchat media before the Beeper chat exists")
	}
	parts := make([]*bridgev2.ConvertedMessagePart, 0, len(media))
	for index, attachment := range media {
		if attachment.ID <= 0 || attachment.Filename == "" ||
			(attachment.Type != "image" && attachment.Type != "sticker" &&
				attachment.Type != "video" && attachment.Type != "audio") ||
			(!strings.HasPrefix(attachment.MIMEType, "image/") &&
				!strings.HasPrefix(attachment.MIMEType, "video/") &&
				!strings.HasPrefix(attachment.MIMEType, "audio/")) {
			return nil, fmt.Errorf("invalid Snapchat media metadata for attachment %d", attachment.ID)
		}
		data, err := c.getMedia(ctx, attachment.ID)
		if err != nil {
			return nil, err
		}
		mxc, encryptedFile, err := c.connector.Bridge.Bot.UploadMedia(
			ctx, portal.MXID, data, attachment.Filename, attachment.MIMEType,
		)
		if err != nil {
			return nil, fmt.Errorf("could not upload Snapchat media %q: %w", attachment.Filename, err)
		}
		eventType, msgType := mediaMessageTypes(attachment.Type)
		c.connector.Bridge.Log.Info().
			Str("chat_id", chatID).
			Str("filename", attachment.Filename).
			Str("event_type", eventType.Type).
			Str("msgtype", string(msgType)).
			Int("bytes", len(data)).
			Msg("Uploaded Snapchat media for Beeper event")
		content := &event.MessageEventContent{
			MsgType: msgType,
			Body:    attachment.Filename,
			Info: &event.FileInfo{
				MimeType: attachment.MIMEType,
				Size:     len(data),
			},
		}
		if encryptedFile == nil {
			content.URL = mxc
		} else {
			content.File = encryptedFile
		}
		parts = append(parts, &bridgev2.ConvertedMessagePart{
			ID:      networkid.PartID(fmt.Sprintf("media-%d", index+1)),
			Type:    eventType,
			Content: content,
		})
	}
	return parts, nil
}

func splitConvertedMessageParts(
	parts []*bridgev2.ConvertedMessagePart,
	maxMediaParts int,
) [][]*bridgev2.ConvertedMessagePart {
	var nonMedia, media []*bridgev2.ConvertedMessagePart
	for _, part := range parts {
		if strings.HasPrefix(string(part.ID), "media-") {
			media = append(media, part)
		} else {
			nonMedia = append(nonMedia, part)
		}
	}
	if len(media) == 0 {
		return [][]*bridgev2.ConvertedMessagePart{nonMedia}
	}

	var batches [][]*bridgev2.ConvertedMessagePart
	for len(media) > 0 {
		batchSize := min(len(media), maxMediaParts)
		batch := make([]*bridgev2.ConvertedMessagePart, 0, batchSize+len(nonMedia))
		if len(batches) == 0 {
			batch = append(batch, nonMedia...)
		}
		batch = append(batch, media[:batchSize]...)
		batches = append(batches, batch)
		media = media[batchSize:]
	}
	return batches
}

func mediaMessageTypes(mediaType string) (event.Type, event.MessageType) {
	switch mediaType {
	case "sticker":
		return event.EventSticker, ""
	case "video":
		return event.EventMessage, event.MsgVideo
	case "audio":
		return event.EventMessage, event.MsgAudio
	default:
		return event.EventMessage, event.MsgImage
	}
}

func mediaSeenKey(messageKey string, media []localMedia) string {
	var builder strings.Builder
	builder.WriteString(messageKey)
	for _, attachment := range media {
		fmt.Fprintf(
			&builder, "\x00%d:%s:%s", attachment.ID, attachment.MIMEType, attachment.Filename,
		)
	}
	return stableID(builder.String())
}

func shouldSkipExistingBridgeMessage(
	message localMessage,
	pendingIndex int,
	forceReplay bool,
) bool {
	if message.IsCustom {
		return false
	}
	return message.BridgeMessageID != "" && pendingIndex < 0 && !forceReplay
}

func messageSenderName(message localMessage, history []localMessage) string {
	if !message.IsCustom {
		return message.Author
	}
	senderName := strings.Join(strings.Fields(message.CustomSender), " ")
	remoteNames := make([]string, 0)
	seen := make(map[string]struct{})
	for _, candidate := range history {
		name := strings.Join(strings.Fields(candidate.Author), " ")
		if candidate.IsCustom || name == "" ||
			strings.EqualFold(name, "you") || strings.EqualFold(name, "ik") {
			continue
		}
		key := strings.ToLower(name)
		if _, exists := seen[key]; exists {
			continue
		}
		seen[key] = struct{}{}
		remoteNames = append(remoteNames, name)
		if senderName != "" && strings.EqualFold(name, senderName) {
			return name
		}
	}
	if len(remoteNames) > 0 {
		return remoteNames[0]
	}
	if senderName != "" {
		return senderName
	}
	return message.Author
}

func (c *Client) queueReaction(
	chatID string,
	message localMessage,
	timestamp time.Time,
	targetID networkid.MessageID,
	history []localMessage,
) error {
	targetPrefix := string(targetID) + "\x00"
	current := make(map[string]localReaction, len(message.Reactions))
	for _, reaction := range message.Reactions {
		author := canonicalReactionAuthor(reaction.Author, history)
		emoji := strings.TrimSpace(reaction.Emoji)
		if author == "" || emoji == "" {
			continue
		}
		current[author+"\x00"+emoji] = localReaction{Author: author, Emoji: emoji}
	}

	c.mu.Lock()
	previous := make(map[string]localReaction)
	for key := range c.seenReactions {
		if !strings.HasPrefix(key, targetPrefix) {
			continue
		}
		parts := strings.SplitN(strings.TrimPrefix(key, targetPrefix), "\x00", 2)
		if len(parts) == 2 {
			previous[parts[0]+"\x00"+parts[1]] = localReaction{Author: parts[0], Emoji: parts[1]}
		}
	}
	c.mu.Unlock()

	for key, old := range previous {
		if _, stillPresent := current[key]; stillPresent {
			continue
		}
		result := c.login.QueueRemoteEvent(&simplevent.Reaction{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventReactionRemove,
				PortalKey: c.portalKey(chatID),
				Sender:    messageEventSender(old.Author, c.login.ID),
				Timestamp: timestamp,
			},
			TargetMessage: targetID,
			EmojiID:       networkid.EmojiID(old.Emoji),
		})
		if result.Error != nil {
			return fmt.Errorf("could not remove Snapchat reaction %q: %w", old.Emoji, result.Error)
		}
		c.mu.Lock()
		delete(c.seenReactions, targetPrefix+key)
		c.mu.Unlock()
	}
	for key, reaction := range current {
		if _, alreadySeen := previous[key]; alreadySeen {
			continue
		}
		result := c.login.QueueRemoteEvent(&simplevent.Reaction{
			EventMeta: simplevent.EventMeta{
				Type:      bridgev2.RemoteEventReaction,
				PortalKey: c.portalKey(chatID),
				Sender:    messageEventSender(reaction.Author, c.login.ID),
				Timestamp: timestamp,
			},
			TargetMessage: targetID,
			EmojiID:       networkid.EmojiID(reaction.Emoji),
			Emoji:         reaction.Emoji,
		})
		if result.Error != nil {
			return fmt.Errorf("could not queue Snapchat reaction %q: %w", reaction.Emoji, result.Error)
		}
		c.mu.Lock()
		if c.seenReactions == nil {
			c.seenReactions = make(map[string]struct{})
		}
		c.seenReactions[targetPrefix+key] = struct{}{}
		c.mu.Unlock()
	}
	return nil
}

func localMessageID(chatID string, message localMessage, timestamp time.Time) networkid.MessageID {
	if message.CustomID != "" {
		return networkid.MessageID(message.CustomID)
	}
	if message.BridgeMessageID != "" {
		return networkid.MessageID(message.BridgeMessageID)
	}
	return networkid.MessageID(stableID(stableMessageKey(
		chatID,
		message.Author,
		strings.TrimSpace(message.Body),
		timestamp,
	)))
}

func (c *Client) getChats(ctx context.Context) ([]localChat, error) {
	var response localChatsResponse
	if err := getJSON(ctx, c.http, c.apiURL()+"/api/chats", &response); err != nil {
		return nil, fmt.Errorf("could not read local Snapchat chats: %w", err)
	}
	return response.Chats, nil
}

func (c *Client) getMessages(ctx context.Context, chatID string) ([]localMessage, error) {
	endpoint := c.apiURL() + "/api/messages?chat=" + url.QueryEscape(chatID) +
		"&include_custom=true"
	var response localMessagesResponse
	if err := getJSON(ctx, c.http, endpoint, &response); err != nil {
		return nil, err
	}
	return response.Messages, nil
}

func (c *Client) getMedia(ctx context.Context, attachmentID int64) ([]byte, error) {
	endpoint := fmt.Sprintf("%s/api/media/%d", c.apiURL(), attachmentID)
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("could not create Snapchat media request: %w", err)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, fmt.Errorf("could not download Snapchat media %d: %w", attachmentID, err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf(
			"could not download Snapchat media %d: local API returned %s",
			attachmentID, response.Status,
		)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxMediaSize+1))
	if err != nil {
		return nil, fmt.Errorf("could not read Snapchat media %d: %w", attachmentID, err)
	}
	if len(data) == 0 {
		return nil, fmt.Errorf("Snapchat media %d is empty", attachmentID)
	}
	if len(data) > maxMediaSize {
		return nil, fmt.Errorf("Snapchat media %d exceeds the 50 MiB limit", attachmentID)
	}
	return data, nil
}

func (c *Client) apiURL() string {
	return strings.TrimRight(c.connector.Config.LocalAPI, "/")
}

func (c *Client) portalKey(chatID string) networkid.PortalKey {
	return networkid.PortalKey{
		ID:       networkid.PortalID(chatID),
		Receiver: c.login.ID,
	}
}

func (c *Client) reportState(state status.BridgeStateEvent, message string) {
	c.mu.Lock()
	if c.last == state && c.lastMsg == message {
		c.mu.Unlock()
		return
	}
	c.last = state
	c.lastMsg = message
	c.mu.Unlock()
	c.login.BridgeState.Send(status.BridgeState{StateEvent: state, Message: message})
}

func (c *Client) setOnline(online bool) {
	c.mu.Lock()
	c.online = online
	c.mu.Unlock()
}

func (c *Client) prunePending(now time.Time) {
	c.mu.Lock()
	defer c.mu.Unlock()
	for key, messages := range c.pending {
		remaining := messages[:0]
		for _, message := range messages {
			if now.Sub(message.created) < 10*time.Minute {
				remaining = append(remaining, message)
			}
		}
		if len(remaining) == 0 {
			delete(c.pending, key)
		} else {
			c.pending[key] = remaining
		}
	}
}

func getJSON(ctx context.Context, client *http.Client, endpoint string, target any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return err
	}
	return doJSON(client, request, target)
}

func postJSON(ctx context.Context, client *http.Client, endpoint string, body any, target any) error {
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(encoded))
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	return doJSON(client, request, target)
}

func doJSON(client *http.Client, request *http.Request, target any) error {
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, maxResponseLen))
	if err != nil {
		return err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		var apiError struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(content, &apiError)
		if apiError.Error == "" {
			apiError.Error = strings.TrimSpace(string(content))
		}
		return fmt.Errorf("local Snapchat API returned HTTP %d: %s", response.StatusCode, apiError.Error)
	}
	if target == nil || len(content) == 0 {
		return nil
	}
	if err := json.Unmarshal(content, target); err != nil {
		return fmt.Errorf("could not parse local Snapchat API response: %w", err)
	}
	return nil
}

func parseTimestamp(value string) (time.Time, error) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999"} {
		if parsed, err := time.Parse(layout, value); err == nil {
			return parsed, nil
		}
	}
	return time.Time{}, fmt.Errorf("unsupported timestamp format")
}

func stableMessageKey(chatID, author, body string, timestamp time.Time) string {
	return fmt.Sprintf("%s\x00%s\x00%s\x00%s", chatID, author, body, timestamp.UTC().Format(time.RFC3339Nano))
}

func messageEventSender(author string, loginID networkid.UserLoginID) bridgev2.EventSender {
	if normalizeAuthor(author) == "You" {
		return bridgev2.EventSender{
			IsFromMe:    true,
			SenderLogin: loginID,
			Sender:      localUserID,
		}
	}
	return bridgev2.EventSender{Sender: makeUserID(author)}
}

func canonicalReactionAuthor(author string, history []localMessage) string {
	author = normalizeAuthor(strings.Join(strings.Fields(author), " "))
	if author == "" || author == "You" {
		return author
	}
	reactionAuthorKey := strings.ToLower(author)
	remoteAuthors := make(map[string]string)
	for _, message := range history {
		candidate := strings.Join(strings.Fields(message.Author), " ")
		if candidate == "" || strings.EqualFold(candidate, "you") ||
			strings.EqualFold(candidate, "ik") {
			continue
		}
		key := strings.ToLower(candidate)
		remoteAuthors[key] = candidate
		if key == reactionAuthorKey {
			return candidate
		}
	}
	if len(remoteAuthors) == 1 {
		return "You"
	}
	return author
}

func findReplyTarget(
	chatID string,
	message localMessage,
	history []localMessage,
) *networkid.MessageOptionalPartID {
	replyBody := strings.Join(strings.Fields(message.ReplyToBody), " ")
	if replyBody == "" {
		return nil
	}
	replyAuthor := normalizeAuthor(message.ReplyToAuthor)
	if replyAuthor == "" {
		return nil
	}
	replyTimestamp, err := parseTimestamp(message.Timestamp)
	if err != nil {
		return nil
	}

	var target *localMessage
	var targetTimestamp time.Time
	for index := range history {
		candidate := &history[index]
		if candidate.IsCustom || candidate.Status == "sending" ||
			strings.Join(strings.Fields(candidate.Body), " ") != replyBody {
			continue
		}
		if !strings.EqualFold(normalizeAuthor(candidate.Author), replyAuthor) {
			continue
		}
		timestamp, err := parseTimestamp(candidate.Timestamp)
		if err != nil || !timestamp.Before(replyTimestamp) {
			continue
		}
		if target == nil || timestamp.After(targetTimestamp) {
			target = candidate
			targetTimestamp = timestamp
		}
	}
	if target == nil {
		return nil
	}

	targetID := networkid.MessageID(stableID(stableMessageKey(
		chatID,
		target.Author,
		strings.TrimSpace(target.Body),
		targetTimestamp,
	)))
	if target.BridgeMessageID != "" {
		targetID = networkid.MessageID(target.BridgeMessageID)
	}
	return &networkid.MessageOptionalPartID{MessageID: targetID}
}

func findLocalReplyTarget(
	chatID string,
	messageID networkid.MessageID,
	history []localMessage,
) *localReplyTarget {
	message := findLocalMessage(chatID, messageID, history)
	if message == nil {
		return nil
	}
	body := strings.TrimSpace(message.Body)
	return &localReplyTarget{
		Author:    normalizeAuthor(message.Author),
		Body:      body,
		Timestamp: message.Timestamp,
		MediaOnly: message.MediaOnly || body == "" && len(message.Media) > 0 ||
			isMediaPlaceholder(body),
	}
}

func findLocalMessage(
	chatID string,
	messageID networkid.MessageID,
	history []localMessage,
) *localMessage {
	if messageID == "" {
		return nil
	}
	for index := range history {
		message := &history[index]
		body := strings.TrimSpace(message.Body)
		if message.IsCustom || body == "" && len(message.Media) == 0 &&
			!message.MediaOnly {
			continue
		}
		localID := message.BridgeMessageID
		if localID == "" {
			timestamp, err := parseTimestamp(message.Timestamp)
			if err != nil {
				continue
			}
			localID = stableID(stableMessageKey(
				chatID,
				message.Author,
				body,
				timestamp,
			))
		}
		if localID == string(messageID) {
			return message
		}
	}
	return nil
}

func isMediaOnlyTarget(
	chatID string,
	messageID networkid.MessageID,
	history []localMessage,
) bool {
	message := findLocalMessage(chatID, messageID, history)
	return message != nil && message.MediaOnly
}

func isMediaPartID(partID string) bool {
	return strings.HasPrefix(partID, "media-")
}

func isMediaPlaceholder(body string) bool {
	switch strings.ToLower(strings.TrimSpace(body)) {
	case "audio", "photo", "video", "sticker":
		return true
	default:
		return false
	}
}

func normalizeAuthor(author string) string {
	author = strings.TrimSpace(author)
	if strings.EqualFold(author, "you") || strings.EqualFold(author, "ik") {
		return "You"
	}
	return author
}

func stableID(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:16])
}

func echoKey(chatID, body string) string {
	return chatID + "\x00" + body
}

func makeUserID(name string) networkid.UserID {
	return networkid.UserID("user:" + base64.RawURLEncoding.EncodeToString([]byte(name)))
}

func decodeUserName(encoded string) (string, error) {
	name, err := base64.RawURLEncoding.DecodeString(encoded)
	return string(name), err
}

var _ bridgev2.NetworkConnector = (*Connector)(nil)
var _ bridgev2.NetworkAPI = (*Client)(nil)
var _ bridgev2.LoginProcess = (*loginProcess)(nil)
var _ bridgev2.NetworkAPIWithUserID = (*Client)(nil)
var _ bridgev2.RedactionHandlingNetworkAPI = (*Client)(nil)

func (c *Client) GetUserID() networkid.UserID {
	return localUserID
}
