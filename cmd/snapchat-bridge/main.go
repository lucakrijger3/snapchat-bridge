package main

import (
	"os"

	"maunium.net/go/mautrix/bridgev2/matrix/mxmain"
	"maunium.net/go/mautrix/id"

	"github.com/lucakrijger3/sh-snapchat"
)

func main() {
	connector := snapchat.NewConnector()
	connector.AutoLoginUserID = id.UserID(os.Getenv("SNAPCHAT_AUTO_LOGIN_USER_ID"))
	main := &mxmain.BridgeMain{
		Name:        "Snapchat Beeper Bridge",
		Description: "A Beeper bridge for a local Snapchat Web session",
		URL:         "https://github.com/lucakrijger3/sh-snapchat",
		Version:     "0.1.0",
		Connector:   connector,
		PostStart:   connector.StartAutomaticLogin,
	}
	main.Run()
}
