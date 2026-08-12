package kernelapp

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"

	tea "charm.land/bubbletea/v2"
	"github.com/charmbracelet/x/ansi"
	"google.golang.org/protobuf/proto"

	protocolv3 "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolv3"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/publictext"
)

const (
	maximumResidentEntries       = 200
	maximumControlItems          = 64
	maximumDraftBytes            = 1 << 20
	maximumHydratedBytes         = 16 << 20 // exact canonical blob hard bound
	maximumCachedEntries         = 512
	maximumCachedEntryBytes      = 8 << 20
	maximumCachedBytes           = 24 << 20 // entry plane plus one maximum blob
	maximumHistoryPage           = 128
	maximumHistoryBytes          = 4 << 20
	maximumLiveToolArgumentBytes = 32 << 10
	maximumTrackedPromptCommands = 128 // mirrors the server's pending-prompt hard bound
)

const liveToolArgumentsTruncated = "… [arguments truncated]"

type Service interface {
	SessionID() string
	ResetConnection()
	Hello(context.Context) (*protocolv3.HelloAccepted, error)
	Snapshot(context.Context, uint32, uint32) (*protocolv3.SnapshotResponse, error)
	LiveControlSnapshot(context.Context) (*protocolv3.LiveControlSnapshotResponse, error)
	Observe(context.Context, uint64, uint64, uint64, uint64, uint64) (*protocolv3.ObservationResponse, error)
	Heartbeat(context.Context) (*protocolv3.HeartbeatResponse, error)
	History(context.Context, *protocolv3.HistoryCursor, uint32, uint32) (*protocolv3.HistoryPageResponse, error)
	Command(context.Context, string, protocolv3.CommandKind, string, string) (*protocolv3.CommandOutcome, error)
	ResolveInteraction(context.Context, string, uint64, uint64, uint64, string, protocolv3.InteractionResolutionDecision) (*protocolv3.CommandOutcome, error)
	QueryCommand(context.Context, string) (*protocolv3.QueryCommandResponse, error)
	ReadContent(context.Context, string, string, uint64) (*protocolv3.CanonicalContentChunk, error)
}

type planCommandService interface {
	CommandWithPlanFields(context.Context, string, protocolv3.CommandKind, string, string, protocolv3.PermissionMode, string, uint64) (*protocolv3.CommandOutcome, error)
}
type planResolutionService interface {
	ResolvePlanQuestion(context.Context, string, string, string, uint64, uint64, *protocolv3.PlanQuestionAnswer) (*protocolv3.ResolvePlanInteractionResponse, error)
	ResolvePlanDraft(context.Context, string, string, string, uint64, uint64, protocolv3.PlanDraftDecision, *string) (*protocolv3.ResolvePlanInteractionResponse, error)
}
type planContentService interface {
	ReadPlanQuestion(context.Context, string) (*protocolv3.PlanQuestionContent, error)
	ReadPlanDraft(context.Context, string, string, uint64, uint32) (*protocolv3.PlanDraftTextChunk, error)
}

type phase uint8

type contentIntegrityError struct{ reason string }

func (e *contentIntegrityError) Error() string { return e.reason }

const (
	phaseConnecting phase = iota + 1
	phaseLoading
	phaseReady
	phaseReconnecting
	phaseFatal
)

type liveDraft struct {
	entryID                string
	turnID                 string
	generationID           string
	scopeKind              protocolv3.ConversationScopeKind
	scopeTaskID            string
	channelKind            protocolv3.LiveChannelKind
	toolCallID             string
	attemptID              string
	blocks                 map[string]string
	toolNames              map[string]string
	toolArguments          map[string]string
	toolArgumentsTruncated map[string]bool
	blockOrdinals          map[string]uint32
	order                  []string
}

type pendingCommand struct {
	id                   string
	kind                 protocolv3.CommandKind
	text                 string
	target               string
	status               protocolv3.CommandStatus
	detach               bool
	interaction          bool
	interactionID        string
	interactionDecision  protocolv3.InteractionResolutionDecision
	writerGeneration     uint64
	controlEpoch         uint64
	controlRevision      uint64
	requestedMode        protocolv3.PermissionMode
	planWorkflowID       string
	planWorkflowRevision uint64
}

type planDraftState struct {
	interactionID string
	digest        string
	total         uint64
	value         []byte
	done          bool
	loading       bool
}

type contentState struct {
	entryID     string
	blockID     string
	digest      string
	total       uint64
	value       []byte
	done        bool
	verified    bool
	truncated   bool
	unavailable bool
}

type Model struct {
	service   Service
	sessionID string
	phase     phase
	failure   string
	width     int
	height    int
	role      protocolv3.AttachmentRole

	entries             map[string]*protocolv3.CanonicalEntry
	order               []string
	entryBytes          int
	control             *protocolv3.CanonicalControl
	writerGeneration    uint64
	older               *protocolv3.HistoryCursor
	event               uint64
	liveEpoch           uint64
	liveRevision        uint64
	live                map[string]*liveDraft
	controlEpoch        uint64
	controlLiveRevision uint64
	currentInteraction  *protocolv3.LiveInteractionView
	content             map[string]*contentState
	permissionMode      protocolv3.PermissionMode
	planQuestion        *protocolv3.PlanQuestionContent
	planDraft           *planDraftState

	draft                  []rune
	cursor                 int
	promptHistory          []string
	historyIndex           int
	historyScratch         string
	pending                *pendingCommand
	trackedPrompts         map[string]pendingCommand
	notice                 string
	scroll                 int
	followTail             bool
	pageLoading            bool
	contentLoading         bool
	reconnectGeneration    uint64
	heartbeatInterval      time.Duration
	preserveLiveOnSnapshot bool
}

type helloMsg struct {
	value      *protocolv3.HelloAccepted
	err        error
	generation uint64
}
type snapshotMsg struct {
	value *protocolv3.SnapshotResponse
	err   error
}
type liveControlSnapshotMsg struct {
	value *protocolv3.LiveControlSnapshotResponse
	err   error
}
type observationMsg struct {
	value *protocolv3.ObservationResponse
	err   error
}
type historyMsg struct {
	value          *protocolv3.HistoryPageResponse
	err            error
	maximumEntries uint32
	maximumBytes   uint32
}
type commandMsg struct {
	value  *protocolv3.CommandOutcome
	err    error
	frozen pendingCommand
}
type queryMsg struct {
	value     *protocolv3.QueryCommandResponse
	err       error
	commandID string
}
type contentMsg struct {
	value            *protocolv3.CanonicalContentChunk
	err              error
	entryID, blockID string
}
type queryDueMsg struct{ commandID string }
type reconnectDueMsg struct{ generation uint64 }
type heartbeatDueMsg struct{ generation uint64 }
type heartbeatMsg struct {
	value      *protocolv3.HeartbeatResponse
	err        error
	generation uint64
}
type planQuestionMsg struct {
	value         *protocolv3.PlanQuestionContent
	err           error
	interactionID string
}
type planDraftMsg struct {
	value         *protocolv3.PlanDraftTextChunk
	err           error
	interactionID string
}
type planResolutionMsg struct {
	value  *protocolv3.ResolvePlanInteractionResponse
	err    error
	frozen pendingCommand
}

func New(service Service) Model {
	sessionID := service.SessionID()
	if sessionID == "" {
		panic("Protocol v3 service session identity is empty")
	}
	return Model{
		service: service, sessionID: sessionID, phase: phaseConnecting, width: 80, height: 24,
		entries: map[string]*protocolv3.CanonicalEntry{}, live: map[string]*liveDraft{}, content: map[string]*contentState{},
		trackedPrompts: map[string]pendingCommand{},
		control:        &protocolv3.CanonicalControl{}, permissionMode: protocolv3.PermissionMode_PERMISSION_MODE_BYPASS_PERMISSIONS,
		followTail: true, historyIndex: -1,
	}
}

func (m Model) Init() tea.Cmd { return m.helloCommand(0) }

func (m Model) Update(message tea.Msg) (tea.Model, tea.Cmd) {
	switch value := message.(type) {
	case helloMsg:
		if value.generation != m.reconnectGeneration {
			return m, nil
		}
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		m.phase, m.role = phaseLoading, value.value.GrantedRole
		m.heartbeatInterval = time.Duration(value.value.HeartbeatIntervalMs) * time.Millisecond
		if err := m.installLiveSnapshot(value.value); err != nil {
			return m.fail(err)
		}
		return m, m.snapshotCommand()
	case snapshotMsg:
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		if err := m.installSnapshot(value.value); err != nil {
			return m.fail(err)
		}
		return m, m.liveControlSnapshotCommand()
	case liveControlSnapshotMsg:
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		if err := m.installLiveControlSnapshot(value.value); err != nil {
			return m.fail(err)
		}
		m.phase = phaseReady
		commands := []tea.Cmd{m.observeCommand(), m.heartbeatAfter()}
		if m.pending != nil {
			commands = append(commands, m.queryCommand(m.pending.id))
		}
		for _, commandID := range m.trackedPromptIDs() {
			commands = append(commands, m.queryCommand(commandID))
		}
		if command := m.nextContentCommand(); command != nil {
			commands = append(commands, command)
		}
		if command := m.nextPlanContentCommand(); command != nil {
			commands = append(commands, command)
		}
		return m, tea.Batch(commands...)
	case observationMsg:
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		if err := m.applyObservation(value.value); err != nil {
			return m.fail(err)
		}
		if value.value.Gap != nil && value.value.Gap.Kind != protocolv3.ObservationGapKind_OBSERVATION_GAP_KIND_UNSPECIFIED {
			switch value.value.Gap.Kind {
			case protocolv3.ObservationGapKind_COMMITTED_GAP:
				m.phase = phaseLoading
				m.preserveLiveOnSnapshot = true
				return m, m.snapshotCommand()
			case protocolv3.ObservationGapKind_LIVE_GAP:
				m.live = map[string]*liveDraft{}
				m.liveEpoch, m.liveRevision = value.value.LiveOwnerEpoch, value.value.Gap.LatestSequence
				return m, m.observeCommand()
			case protocolv3.ObservationGapKind_LIVE_CONTROL_GAP:
				m.currentInteraction = nil
				return m, m.liveControlSnapshotCommand()
			default:
				return m.fail(fmt.Errorf("observation GAP kind is unknown"))
			}
		}
		commands := []tea.Cmd{m.observeCommand()}
		if command := m.nextContentCommand(); command != nil {
			commands = append(commands, command)
		}
		if command := m.nextPlanContentCommand(); command != nil {
			commands = append(commands, command)
		}
		return m, tea.Batch(commands...)
	case planQuestionMsg:
		if m.control == nil || m.control.OpenPlanInteraction == nil || m.control.OpenPlanInteraction.InteractionId != value.interactionID {
			return m, nil
		}
		if value.err != nil {
			m.notice = "Plan question content unavailable"
			return m, nil
		}
		if err := validatePlanQuestion(value.value, value.interactionID); err != nil {
			return m.fail(err)
		}
		m.planQuestion = proto.Clone(value.value).(*protocolv3.PlanQuestionContent)
		return m, nil
	case planDraftMsg:
		if m.planDraft == nil || m.planDraft.interactionID != value.interactionID {
			return m, nil
		}
		m.planDraft.loading = false
		if value.err != nil {
			m.notice = "Plan draft content unavailable"
			return m, nil
		}
		if err := m.applyPlanDraftChunk(value.value); err != nil {
			return m.fail(err)
		}
		return m, m.nextPlanContentCommand()
	case planResolutionMsg:
		if m.pending == nil || m.pending.id != value.frozen.id {
			return m, nil
		}
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		m.notice = "Plan decision accepted"
		if value.value.ResumePermissionMode != protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED {
			m.permissionMode = value.value.ResumePermissionMode
		}
		m.pending = nil
		m.draft = nil
		m.cursor = 0
		return m, m.snapshotCommand()
	case historyMsg:
		m.pageLoading = false
		if value.err != nil {
			m.notice = "History page unavailable"
			return m, nil
		}
		if len(value.value.Entries) > int(value.maximumEntries) || proto.Size(value.value) > int(value.maximumBytes) {
			return m.fail(fmt.Errorf("history page exceeded its requested bound"))
		}
		if err := m.applyHistory(value.value); err != nil {
			return m.fail(err)
		}
		if command := m.nextContentCommand(); command != nil {
			return m, command
		}
		return m, nil
	case contentMsg:
		m.contentLoading = false
		if value.err != nil {
			if !isCanonicalContentUnavailable(value.err) {
				m.notice = "Large content read failed"
				return m, nil
			}
			key := contentKey(value.entryID, value.blockID)
			m.content[key] = &contentState{
				entryID: value.entryID, blockID: value.blockID,
				done: true, unavailable: true,
			}
			m.notice = "Large content unavailable"
			return m, nil
		}
		if err := m.applyContent(value); err != nil {
			var integrity *contentIntegrityError
			if errors.As(err, &integrity) {
				key := contentKey(value.entryID, value.blockID)
				m.content[key] = &contentState{
					entryID: value.entryID, blockID: value.blockID,
					done: true, unavailable: true,
				}
				m.notice = "Large content failed integrity verification"
				return m, m.nextContentCommand()
			}
			return m.fail(err)
		}
		return m, m.nextContentCommand()
	case commandMsg:
		if m.pending == nil || m.pending.id != value.frozen.id {
			return m, nil
		}
		if value.err != nil {
			// A serial transport error after write is an ACK-unknown outcome.
			// Keep the frozen command identity and query canonical state after
			// reconnect; never create a second semantic command.
			return m.scheduleReconnect(value.err)
		}
		m.pending.status, m.pending.target = value.value.Status, value.value.TargetId
		m.notice = publictext.Transform(value.value.PublicMessage)
		if value.value.Status == protocolv3.CommandStatus_REJECTED {
			if isPromptCommand(value.frozen.kind) {
				m.restoreRejectedDraft(value.frozen.text)
			}
			m.pending = nil
			return m, nil
		}
		if value.frozen.detach && value.value.Status == protocolv3.CommandStatus_SUCCEEDED {
			return m, tea.Quit
		}
		if value.value.Status == protocolv3.CommandStatus_SUCCEEDED {
			if isPromptCommand(value.frozen.kind) {
				m.acceptPromptHistory(value.frozen.text)
			}
			m.pending = nil
			return m, nil
		}
		if isPromptCommand(value.frozen.kind) {
			tracked := *m.pending
			m.trackedPrompts[tracked.id] = tracked
			m.pending = nil
			if promptIngressAccepted(value.value.PublicCode) {
				m.acceptPromptHistory(tracked.text)
				delete(m.trackedPrompts, tracked.id)
				return m, nil
			}
		}
		return m, queryAfter(value.frozen.id)
	case queryDueMsg:
		if !m.isCommandAwaitingQuery(value.commandID) {
			return m, nil
		}
		return m, m.queryCommand(value.commandID)
	case queryMsg:
		command, isPending, exists := m.commandAwaitingQuery(value.commandID)
		if !exists {
			return m, nil
		}
		if value.err != nil {
			return m.scheduleReconnect(value.err)
		}
		if !value.value.Found || value.value.Outcome == nil {
			if isPending && command.interaction && m.currentInteraction != nil &&
				m.currentInteraction.InteractionId == command.interactionID &&
				m.controlEpoch == command.controlEpoch &&
				m.controlLiveRevision == command.controlRevision {
				return m, m.resolveInteractionCommand(command)
			}
			if isPromptCommand(command.kind) {
				m.restoreRejectedDraft(command.text)
			}
			m.notice = "Command was not accepted"
			m.clearAwaitingCommand(value.commandID, isPending)
			return m, nil
		}
		if value.value.Outcome.Status == protocolv3.CommandStatus_PENDING {
			if isPromptCommand(command.kind) {
				if isPending {
					m.trackedPrompts[command.id] = command
					m.pending = nil
				}
				if promptIngressAccepted(value.value.Outcome.PublicCode) {
					m.notice = publictext.Transform(value.value.Outcome.PublicMessage)
					m.acceptPromptHistory(command.text)
					delete(m.trackedPrompts, command.id)
					return m, nil
				}
			}
			return m, queryAfter(value.commandID)
		}
		outcome := value.value.Outcome
		m.notice = publictext.Transform(outcome.PublicMessage)
		if outcome.Status == protocolv3.CommandStatus_SUCCEEDED {
			if outcome.ResumePermissionMode != protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED {
				m.permissionMode = outcome.ResumePermissionMode
			}
			if isPromptCommand(command.kind) {
				m.acceptPromptHistory(command.text)
			}
			if command.detach {
				m.clearAwaitingCommand(value.commandID, isPending)
				return m, tea.Quit
			}
		} else if isPromptCommand(command.kind) {
			m.restoreRejectedDraft(command.text)
		}
		m.clearAwaitingCommand(value.commandID, isPending)
		return m, nil
	case reconnectDueMsg:
		if value.generation != m.reconnectGeneration || m.phase != phaseReconnecting {
			return m, nil
		}
		m.phase = phaseConnecting
		return m, m.helloCommand(value.generation)
	case heartbeatDueMsg:
		if value.generation != m.reconnectGeneration || m.phase != phaseReady {
			return m, nil
		}
		return m, m.heartbeatCommand(value.generation)
	case heartbeatMsg:
		if value.generation != m.reconnectGeneration {
			return m, nil
		}
		if value.err != nil || value.value == nil || !value.value.Active {
			if value.err == nil {
				value.err = fmt.Errorf("Protocol v3 attachment is inactive")
			}
			return m.scheduleReconnect(value.err)
		}
		return m, m.heartbeatAfter()
	case tea.WindowSizeMsg:
		m.width, m.height = max(value.Width, 1), max(value.Height, 1)
		return m, nil
	case tea.MouseWheelMsg:
		if value.Mouse().Button == tea.MouseWheelUp {
			m.scroll += 3
			m.followTail = false
		} else if value.Mouse().Button == tea.MouseWheelDown {
			m.scroll = max(0, m.scroll-3)
			m.followTail = m.scroll == 0
		}
		return m, nil
	case tea.PasteMsg:
		if m.canEdit() {
			m.insertText(value.Content)
		}
		return m, nil
	case tea.KeyPressMsg:
		return m.handleKey(value)
	}
	return m, nil
}

func (m Model) View() tea.View {
	view := tea.NewView(m.render())
	view.AltScreen = true
	view.MouseMode = tea.MouseModeCellMotion
	view.WindowTitle = "Pulsara"
	return view
}

func (m Model) handleKey(message tea.KeyPressMsg) (tea.Model, tea.Cmd) {
	key := message.Keystroke()
	if m.phase == phaseReady && m.pending == nil {
		if interaction := m.openPlanInteraction(); interaction != nil {
			switch interaction.Kind {
			case "QUESTION":
				if m.planQuestion != nil {
					if len(key) == 1 && key[0] >= '1' && key[0] <= '3' {
						ordinal := int(key[0] - '1')
						if ordinal < len(m.planQuestion.Options) {
							return m.beginPlanQuestionOption(uint32(ordinal))
						}
					}
					if key == "enter" && m.planQuestion.AllowFreeText && strings.TrimSpace(string(m.draft)) != "" {
						return m.beginPlanQuestionText(string(m.draft))
					}
				}
			case "DRAFT_REVIEW":
				switch key {
				case "a", "A":
					return m.beginPlanDraftDecision(protocolv3.PlanDraftDecision_PLAN_DRAFT_APPROVE, nil)
				case "c", "C":
					return m.beginPlanDraftDecision(protocolv3.PlanDraftDecision_PLAN_DRAFT_CANCEL, nil)
				case "r", "R":
					var feedback *string
					if value := strings.TrimSpace(string(m.draft)); value != "" {
						feedback = &value
					}
					return m.beginPlanDraftDecision(protocolv3.PlanDraftDecision_PLAN_DRAFT_REVISE, feedback)
				}
			}
		}
	}
	if m.phase == phaseReady && m.currentInteraction != nil && m.pending == nil {
		switch key {
		case "y", "Y", "enter":
			return m.beginInteraction(protocolv3.InteractionResolutionDecision_INTERACTION_ALLOW)
		case "n", "N", "esc":
			return m.beginInteraction(protocolv3.InteractionResolutionDecision_INTERACTION_DENY)
		}
	}
	switch key {
	case "ctrl+p":
		if m.phase == phaseReady && m.pending == nil {
			m.permissionMode = nextPermissionMode(m.permissionMode)
			m.notice = "Permission: " + permissionModeLabel(m.permissionMode)
		}
		return m, nil
	case "ctrl+l":
		if m.phase == phaseReady && m.pending == nil && m.activePlanWorkflow() == nil && m.openPlanInteraction() == nil {
			return m.beginTargetedCommand(protocolv3.CommandKind_ENTER_PLAN, strings.TrimSpace(string(m.draft)), "", false)
		}
	case "ctrl+x":
		if workflow := m.activePlanWorkflow(); workflow != nil && m.pending == nil {
			return m.beginPlanExit(protocolv3.CommandKind_CANCEL_PLAN, workflow)
		}
	case "ctrl+f":
		if workflow := m.activePlanWorkflow(); workflow != nil && m.pending == nil {
			return m.beginPlanExit(protocolv3.CommandKind_FORCE_EXIT_PLAN, workflow)
		}
	case "ctrl+d":
		if m.phase != phaseReady {
			return m, tea.Quit
		}
		return m.beginCommand(protocolv3.CommandKind_DETACH, "", true)
	case "ctrl+c":
		if m.phase == phaseReady {
			return m.beginCommand(protocolv3.CommandKind_STOP_ACTIVE_TURN, "", false)
		}
	case "pgup":
		m.scroll += max(1, m.transcriptHeight()-1)
		m.followTail = false
		if m.older != nil && !m.pageLoading && m.historyCapacity() > 0 {
			m.pageLoading = true
			return m, m.historyCommand()
		} else if m.older != nil && m.historyCapacity() == 0 {
			m.notice = "History cache limit reached"
		}
	case "pgdown":
		m.scroll = max(0, m.scroll-max(1, m.transcriptHeight()-1))
		m.followTail = m.scroll == 0
	case "end":
		m.scroll, m.followTail = 0, true
	case "up":
		if m.canEdit() {
			m.previousPrompt()
		}
	case "down":
		if m.canEdit() {
			m.nextPrompt()
		}
	case "left":
		m.cursor = max(0, m.cursor-1)
	case "right":
		m.cursor = min(len(m.draft), m.cursor+1)
	case "backspace":
		if m.canEdit() && m.cursor > 0 {
			m.draft = append(m.draft[:m.cursor-1], m.draft[m.cursor:]...)
			m.cursor--
			m.exitHistoryTraversal()
		}
	case "delete":
		if m.canEdit() && m.cursor < len(m.draft) {
			m.draft = append(m.draft[:m.cursor], m.draft[m.cursor+1:]...)
			m.exitHistoryTraversal()
		}
	case "alt+enter", "shift+enter":
		if m.canEdit() {
			m.insertText("\n")
		}
	case "enter":
		if m.canSubmit() && m.openPlanInteraction() == nil && strings.TrimSpace(string(m.draft)) != "" {
			kind := protocolv3.CommandKind_SUBMIT_PROMPT
			target := ""
			if activeTurnID, present := m.activeRootTurnID(); present {
				kind = protocolv3.CommandKind_STEER_ACTIVE_TURN
				target = activeTurnID
			}
			return m.beginTargetedCommand(kind, string(m.draft), target, false)
		}
	default:
		text := message.Key().Text
		if m.canEdit() && text != "" {
			m.insertText(text)
		}
	}
	return m, nil
}

func (m Model) beginCommand(kind protocolv3.CommandKind, text string, detach bool) (tea.Model, tea.Cmd) {
	return m.beginTargetedCommand(kind, text, "", detach)
}

func (m Model) beginTargetedCommand(kind protocolv3.CommandKind, text, target string, detach bool) (tea.Model, tea.Cmd) {
	if m.pending != nil {
		m.notice = "A command is already pending"
		return m, nil
	}
	command := pendingCommand{id: newID("terminal-command"), kind: kind, text: text, target: target, status: protocolv3.CommandStatus_PENDING, detach: detach, requestedMode: m.permissionMode}
	m.pending = &command
	if kind == protocolv3.CommandKind_SUBMIT_PROMPT || kind == protocolv3.CommandKind_STEER_ACTIVE_TURN {
		m.draft = nil
		m.cursor = 0
		m.exitHistoryTraversal()
	}
	return m, m.commandCommand(command, kind)
}

func (m Model) beginInteraction(decision protocolv3.InteractionResolutionDecision) (tea.Model, tea.Cmd) {
	if m.pending != nil || m.currentInteraction == nil {
		return m, nil
	}
	command := pendingCommand{
		id: newID("terminal-interaction-command"), status: protocolv3.CommandStatus_PENDING,
		interaction: true, interactionID: m.currentInteraction.InteractionId,
		interactionDecision: decision, writerGeneration: m.writerGeneration,
		controlEpoch: m.controlEpoch, controlRevision: m.controlLiveRevision,
	}
	m.pending = &command
	return m, m.resolveInteractionCommand(command)
}

func (m Model) beginPlanExit(kind protocolv3.CommandKind, workflow *protocolv3.PlanWorkflowControl) (tea.Model, tea.Cmd) {
	command := pendingCommand{
		id: newID("terminal-plan-command"), kind: kind,
		status:               protocolv3.CommandStatus_PENDING,
		planWorkflowID:       workflow.WorkflowId,
		planWorkflowRevision: workflow.WorkflowRevision,
		requestedMode:        m.permissionMode,
	}
	m.pending = &command
	return m, m.commandCommand(command, kind)
}

func (m Model) beginPlanQuestionOption(ordinal uint32) (tea.Model, tea.Cmd) {
	answer := &protocolv3.PlanQuestionAnswer{Answer: &protocolv3.PlanQuestionAnswer_OptionOrdinal{OptionOrdinal: ordinal}}
	return m.beginPlanResolution(answer, protocolv3.PlanDraftDecision_PLAN_DRAFT_DECISION_UNSPECIFIED, nil)
}

func (m Model) beginPlanQuestionText(value string) (tea.Model, tea.Cmd) {
	answer := &protocolv3.PlanQuestionAnswer{Answer: &protocolv3.PlanQuestionAnswer_FreeText{FreeText: value}}
	return m.beginPlanResolution(answer, protocolv3.PlanDraftDecision_PLAN_DRAFT_DECISION_UNSPECIFIED, nil)
}

func (m Model) beginPlanDraftDecision(decision protocolv3.PlanDraftDecision, feedback *string) (tea.Model, tea.Cmd) {
	return m.beginPlanResolution(nil, decision, feedback)
}

func (m Model) beginPlanResolution(answer *protocolv3.PlanQuestionAnswer, decision protocolv3.PlanDraftDecision, feedback *string) (tea.Model, tea.Cmd) {
	workflow, interaction := m.activePlanWorkflow(), m.openPlanInteraction()
	if workflow == nil || interaction == nil || m.pending != nil {
		return m, nil
	}
	command := pendingCommand{
		id: newID("terminal-plan-resolution"), status: protocolv3.CommandStatus_PENDING,
		interactionID: interaction.InteractionId, writerGeneration: m.writerGeneration,
		planWorkflowID: workflow.WorkflowId, planWorkflowRevision: workflow.WorkflowRevision,
	}
	m.pending = &command
	return m, m.resolvePlanCommand(command, answer, decision, feedback)
}

func (m *Model) installSnapshot(response *protocolv3.SnapshotResponse) error {
	if response == nil || response.Snapshot == nil || response.Snapshot.SessionId != m.sessionID || response.Snapshot.WriterGeneration == 0 || response.Snapshot.SnapshotFingerprint == "" || response.Snapshot.Control == nil {
		return fmt.Errorf("canonical snapshot is incomplete")
	}
	if canonicalSnapshotFingerprint(response.Snapshot) != response.Snapshot.SnapshotFingerprint {
		return fmt.Errorf("canonical snapshot fingerprint is invalid")
	}
	if err := validateCanonicalControl(response.Snapshot.Control); err != nil {
		return err
	}
	m.entries = map[string]*protocolv3.CanonicalEntry{}
	m.order = nil
	m.entryBytes = 0
	for _, entry := range response.Snapshot.Entries {
		if err := m.installEntry(entry); err != nil {
			return err
		}
	}
	m.control = proto.Clone(response.Snapshot.Control).(*protocolv3.CanonicalControl)
	m.syncPlanControl()
	m.writerGeneration = response.Snapshot.WriterGeneration
	m.older = cloneCursor(response.Snapshot.OlderHistoryCursor)
	m.event = response.Snapshot.EventSequenceCut
	if !m.preserveLiveOnSnapshot {
		m.live = map[string]*liveDraft{}
	}
	m.preserveLiveOnSnapshot = false
	return nil
}

func (m *Model) installLiveControlSnapshot(response *protocolv3.LiveControlSnapshotResponse) error {
	if response == nil || response.Snapshot == nil || response.Snapshot.SessionId != m.sessionID || response.Snapshot.OwnerEpoch == 0 {
		return fmt.Errorf("live-control snapshot is incomplete")
	}
	m.controlEpoch = response.Snapshot.OwnerEpoch
	m.controlLiveRevision = response.Snapshot.LiveRevision
	if response.Snapshot.CurrentInteraction == nil {
		m.currentInteraction = nil
	} else {
		if err := validateLiveInteraction(response.Snapshot.CurrentInteraction); err != nil {
			return err
		}
		m.currentInteraction = proto.Clone(response.Snapshot.CurrentInteraction).(*protocolv3.LiveInteractionView)
	}
	return nil
}

func (m *Model) applyObservation(response *protocolv3.ObservationResponse) error {
	if response == nil || response.ThroughEventSequence < m.event {
		return fmt.Errorf("committed observation regressed")
	}
	if response.Gap != nil && response.Gap.Kind != protocolv3.ObservationGapKind_OBSERVATION_GAP_KIND_UNSPECIFIED {
		return nil
	}
	expected := m.event + 1
	for _, projection := range response.Committed {
		if projection.EventSequence != expected {
			return fmt.Errorf("committed observation is not contiguous")
		}
		contract, known := protocolv3.ExpectedProjectionContract(projection.EventType)
		if !known || projection.SubjectId == "" || projection.SubjectSlot != contract.SubjectSlot || projection.ProjectionKind != contract.Kind {
			return fmt.Errorf("committed observation projection contract is invalid")
		}
		expected++
		switch projection.ProjectionKind {
		case protocolv3.ObservationProjectionKind_IMMUTABLE_ENTRY:
			if err := m.installEntry(projection.Entry); err != nil {
				return err
			}
		case protocolv3.ObservationProjectionKind_CURRENT_CONTROL:
			if projection.CurrentControl == nil {
				return fmt.Errorf("control observation is empty")
			}
			if err := validateCanonicalControl(projection.CurrentControl); err != nil {
				return err
			}
			m.control = proto.Clone(projection.CurrentControl).(*protocolv3.CanonicalControl)
			m.syncPlanControl()
		case protocolv3.ObservationProjectionKind_EVENT_ONLY:
		default:
			return fmt.Errorf("observation projection kind is unknown")
		}
	}
	if len(response.Committed) > 0 && expected-1 != response.ThroughEventSequence {
		return fmt.Errorf("committed observation cut is incomplete")
	}
	m.event = response.ThroughEventSequence
	if m.liveEpoch != 0 && response.LiveOwnerEpoch != m.liveEpoch {
		return fmt.Errorf("live owner changed without GAP")
	}
	if response.LiveOwnerEpoch != 0 {
		m.liveEpoch = response.LiveOwnerEpoch
	}
	type delivery struct {
		revision   uint64
		event      *protocolv3.LiveEventProjection
		settlement *protocolv3.LiveGenerationSettlement
	}
	deliveries := make([]delivery, 0, len(response.Live)+len(response.Settlements))
	for _, event := range response.Live {
		deliveries = append(deliveries, delivery{revision: event.LiveRevision, event: event})
	}
	for _, settlement := range response.Settlements {
		deliveries = append(deliveries, delivery{revision: settlement.LiveRevision, settlement: settlement})
	}
	sort.Slice(deliveries, func(i, j int) bool { return deliveries[i].revision < deliveries[j].revision })
	for _, item := range deliveries {
		if item.revision != m.liveRevision+1 {
			return fmt.Errorf("live observation is not contiguous")
		}
		m.liveRevision = item.revision
		if item.event != nil {
			if item.event.OwnerEpoch != m.liveEpoch {
				return fmt.Errorf("live observation attribution is invalid")
			}
			if err := m.applyLive(item.event); err != nil {
				return err
			}
		} else if err := m.applySettlement(item.settlement); err != nil {
			return err
		}
	}
	if response.ThroughLiveRevision != m.liveRevision {
		return fmt.Errorf("live observation cut is incomplete")
	}
	if response.LiveControlOwnerEpoch != m.controlEpoch {
		return fmt.Errorf("live-control owner changed without GAP")
	}
	for _, event := range response.LiveControl {
		if event.OwnerEpoch != m.controlEpoch || event.LiveRevision != m.controlLiveRevision+1 {
			return fmt.Errorf("live-control observation attribution is invalid")
		}
		m.controlLiveRevision = event.LiveRevision
		if err := m.applyLiveControl(event); err != nil {
			return err
		}
	}
	if response.ThroughLiveControlRevision != m.controlLiveRevision {
		return fmt.Errorf("live-control observation cut is incomplete")
	}
	return nil
}

func (m *Model) installLiveSnapshot(hello *protocolv3.HelloAccepted) error {
	if hello == nil || hello.LiveSnapshot == nil || hello.LiveOwnerEpoch == 0 ||
		hello.LiveSnapshot.OwnerEpoch != hello.LiveOwnerEpoch ||
		hello.LiveSnapshot.ThroughRevision != hello.LiveRevision {
		return fmt.Errorf("live bootstrap snapshot attribution is invalid")
	}
	snapshot := hello.LiveSnapshot
	m.live = map[string]*liveDraft{}
	m.liveEpoch = snapshot.OwnerEpoch
	m.liveRevision = 0
	if snapshot.TruncatedBefore {
		// A retained suffix cannot safely reconstruct a draft whose Start may
		// have been evicted. Canonical snapshot installation still wins; the
		// client resumes live observation at the exact captured cut.
		m.liveRevision = snapshot.ThroughRevision
		return nil
	}
	type delivery struct {
		revision   uint64
		event      *protocolv3.LiveEventProjection
		settlement *protocolv3.LiveGenerationSettlement
	}
	deliveries := make([]delivery, 0, len(snapshot.Events)+len(snapshot.Settlements))
	for _, event := range snapshot.Events {
		if event == nil {
			return fmt.Errorf("live bootstrap event is absent")
		}
		deliveries = append(deliveries, delivery{revision: event.LiveRevision, event: event})
	}
	for _, settlement := range snapshot.Settlements {
		if settlement == nil {
			return fmt.Errorf("live bootstrap settlement is absent")
		}
		deliveries = append(deliveries, delivery{revision: settlement.LiveRevision, settlement: settlement})
	}
	sort.Slice(deliveries, func(i, j int) bool { return deliveries[i].revision < deliveries[j].revision })
	if len(deliveries) == 0 {
		if snapshot.ThroughRevision != 0 || snapshot.RetainedFromRevision != 1 {
			return fmt.Errorf("empty live bootstrap cut is invalid")
		}
		return nil
	}
	if snapshot.RetainedFromRevision != 1 || deliveries[0].revision != 1 {
		return fmt.Errorf("live bootstrap prefix is incomplete")
	}
	for _, item := range deliveries {
		if item.revision != m.liveRevision+1 {
			return fmt.Errorf("live bootstrap delivery is not contiguous")
		}
		m.liveRevision = item.revision
		if item.event != nil {
			if item.event.OwnerEpoch != m.liveEpoch {
				return fmt.Errorf("live bootstrap event attribution is invalid")
			}
			if err := m.applyLive(item.event); err != nil {
				return err
			}
		} else {
			if item.settlement.OwnerEpoch != m.liveEpoch {
				return fmt.Errorf("live bootstrap settlement attribution is invalid")
			}
			if err := m.applySettlement(item.settlement); err != nil {
				return err
			}
		}
	}
	if m.liveRevision != snapshot.ThroughRevision {
		return fmt.Errorf("live bootstrap cut is incomplete")
	}
	return nil
}

func validateCanonicalControl(control *protocolv3.CanonicalControl) error {
	if control == nil || (control.SessionLifecycle != "OPEN" && control.SessionLifecycle != "CLOSED") {
		return fmt.Errorf("canonical session lifecycle is invalid")
	}
	if len(control.ActiveTurns) > maximumControlItems || len(control.PromptQueue) > maximumControlItems || len(control.ToolAttempts) > maximumControlItems || len(control.SubagentTasks) > maximumControlItems || len(control.Jobs) > maximumControlItems {
		return fmt.Errorf("canonical control section exceeded its hard item bound")
	}
	rootRunning := 0
	taskTurns := map[string]struct{}{}
	for _, turn := range control.ActiveTurns {
		if turn == nil || turn.TurnId == "" || turn.Status != "RUNNING" || turn.AcceptedAtUtc == "" {
			return fmt.Errorf("canonical active-turn control is invalid")
		}
		switch turn.ScopeKind {
		case protocolv3.ConversationScopeKind_ROOT:
			if turn.ScopeSubagentTaskId != "" {
				return fmt.Errorf("canonical ROOT turn carries task scope")
			}
			rootRunning++
		case protocolv3.ConversationScopeKind_SUBAGENT_TASK:
			if turn.ScopeSubagentTaskId == "" {
				return fmt.Errorf("canonical task turn lacks task scope")
			}
			if _, duplicate := taskTurns[turn.ScopeSubagentTaskId]; duplicate {
				return fmt.Errorf("canonical task scope has duplicate active turns")
			}
			taskTurns[turn.ScopeSubagentTaskId] = struct{}{}
		default:
			return fmt.Errorf("canonical active-turn scope is unknown")
		}
		if err := validateRunPermission(turn.Permission); err != nil {
			return err
		}
	}
	if rootRunning > 1 {
		return fmt.Errorf("canonical control has multiple active ROOT turns")
	}
	if control.PromptQueueTotalCount < uint64(len(control.PromptQueue)) {
		return fmt.Errorf("canonical prompt queue count is invalid")
	}
	lastQueue := uint64(0)
	for _, item := range control.PromptQueue {
		if item == nil || item.QueueItemId == "" || item.Status != "PENDING" || item.QueueSequence <= lastQueue {
			return fmt.Errorf("canonical prompt queue control is invalid")
		}
		if err := validateContentReference(item.Content); err != nil {
			return err
		}
		switch item.DeliveryMode {
		case "NEW_TURN":
			if item.TargetTurnId != "" {
				return fmt.Errorf("NEW_TURN queue item carries a target turn")
			}
		case "STEER_ACTIVE_TURN":
			if item.TargetTurnId == "" {
				return fmt.Errorf("steer queue item lacks a target turn")
			}
		default:
			return fmt.Errorf("canonical prompt delivery mode is unknown")
		}
		if item.DeliveryMode == "NEW_TURN" {
			if err := validateRunPermission(item.Permission); err != nil {
				return err
			}
		} else if item.Permission != nil {
			return fmt.Errorf("steer queue item carries permission state")
		}
		lastQueue = item.QueueSequence
	}
	if workflow := control.ActivePlanWorkflow; workflow != nil {
		if workflow.WorkflowId == "" || workflow.WorkflowOrdinal == 0 || workflow.WorkflowRevision == 0 || workflow.Status != "ACTIVE" || workflow.ResumePermissionMode == protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED {
			return fmt.Errorf("canonical active Plan workflow is invalid")
		}
	}
	if interaction := control.OpenPlanInteraction; interaction != nil {
		if control.ActivePlanWorkflow == nil || interaction.InteractionId == "" || interaction.WorkflowId != control.ActivePlanWorkflow.WorkflowId || interaction.Status != "OPEN" || interaction.InteractionOrdinal == 0 || interaction.TypedContentFingerprint == "" || (interaction.Kind != "QUESTION" && interaction.Kind != "DRAFT_REVIEW") {
			return fmt.Errorf("canonical open Plan interaction is invalid")
		}
		if interaction.Kind == "DRAFT_REVIEW" && (interaction.DraftUtf8Digest == "" || interaction.DraftUtf8Size == 0) {
			return fmt.Errorf("canonical Plan draft identity is invalid")
		}
	}
	for _, attempt := range control.ToolAttempts {
		if attempt == nil || attempt.AttemptId == "" || attempt.AssistantEntryId == "" || attempt.ToolCallId == "" || attempt.ResultState != "" || attempt.ResultEntryId != "" {
			return fmt.Errorf("canonical active tool-attempt control is invalid")
		}
	}
	for _, task := range control.SubagentTasks {
		if task == nil || task.TaskId == "" || task.Objective == "" || (task.Status != "PENDING" && task.Status != "ACTIVE") {
			return fmt.Errorf("canonical subagent-task control is invalid")
		}
	}
	validHandlers := map[string]struct{}{
		"BACKGROUND_COMPACTION": {}, "POST_COMPACTION_MEMORY_EXTRACTION": {},
		"MEMORY_GOVERNANCE": {}, "MEMORY_INDEX_REFRESH": {},
	}
	for _, job := range control.Jobs {
		if job == nil {
			return fmt.Errorf("canonical durable-job control is invalid")
		}
		_, knownHandler := validHandlers[job.HandlerType]
		if job.JobId == "" || !knownHandler || (job.Status != "PENDING" && job.Status != "ACTIVE") || job.MaximumAttempts == 0 || job.AttemptCount > job.MaximumAttempts {
			return fmt.Errorf("canonical durable-job control is invalid")
		}
	}
	if len(control.MemoryFreshness) != 2 {
		return fmt.Errorf("canonical memory freshness must contain exact two channels")
	}
	channels := map[string]struct{}{}
	for _, freshness := range control.MemoryFreshness {
		if freshness == nil || (freshness.Channel != "FTS" && freshness.Channel != "VECTOR") || freshness.AppliedGeneration > freshness.DesiredGeneration || freshness.HandlerContract == "" {
			return fmt.Errorf("canonical memory freshness control is invalid")
		}
		if _, duplicate := channels[freshness.Channel]; duplicate {
			return fmt.Errorf("canonical memory freshness channel is duplicated")
		}
		channels[freshness.Channel] = struct{}{}
	}
	return nil
}

func validateRunPermission(value *protocolv3.RunPermissionProjection) error {
	if value == nil || value.PermissionSnapshotId == "" || value.SnapshotFingerprint == "" || value.ContractId == "" || value.ContractFingerprint == "" || value.RequestedMode == protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED || value.EffectiveMode == protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED {
		return fmt.Errorf("canonical run permission is incomplete")
	}
	plan := value.Overlay == "PLAN_READ_ONLY"
	if plan != (value.PlanWorkflowId != "" && value.PlanWorkflowRevision > 0) {
		return fmt.Errorf("canonical run permission Plan union is invalid")
	}
	if plan && value.EffectiveMode != protocolv3.PermissionMode_PERMISSION_MODE_READ_ONLY {
		return fmt.Errorf("canonical Plan permission is not read-only")
	}
	if !plan && value.Overlay != "NONE" {
		return fmt.Errorf("canonical run permission overlay is unknown")
	}
	return nil
}

func validatePlanQuestion(value *protocolv3.PlanQuestionContent, interactionID string) error {
	if value == nil || value.InteractionId != interactionID || value.Question == "" || value.TypedContentFingerprint == "" || len(value.Options) > 3 {
		return fmt.Errorf("Plan question content is invalid")
	}
	if len(value.Options) == 1 || (!value.AllowFreeText && len(value.Options) == 0) {
		return fmt.Errorf("Plan question option union is invalid")
	}
	recommended := 0
	for index, option := range value.Options {
		if option == nil || option.Ordinal != uint32(index) || option.Label == "" {
			return fmt.Errorf("Plan question option is invalid")
		}
		if option.Recommended {
			recommended++
		}
	}
	if len(value.Options) > 0 && recommended != 1 {
		return fmt.Errorf("Plan question recommendation is invalid")
	}
	return nil
}

func (m *Model) applyPlanDraftChunk(chunk *protocolv3.PlanDraftTextChunk) error {
	state := m.planDraft
	if state == nil || chunk == nil || chunk.InteractionId != state.interactionID || chunk.OffsetUtf8Bytes != uint64(len(state.value)) || chunk.PlanUtf8Digest != state.digest || chunk.PlanUtf8Size != state.total || !utf8.ValidString(chunk.Body) {
		return fmt.Errorf("Plan draft chunk identity is invalid")
	}
	state.value = append(state.value, []byte(chunk.Body)...)
	if uint64(len(state.value)) != chunk.NextOffsetUtf8Bytes || uint64(len(state.value)) > state.total {
		return fmt.Errorf("Plan draft chunk range is invalid")
	}
	if chunk.Eof {
		if uint64(len(state.value)) != state.total || planDraftDigest(state.value) != state.digest {
			return fmt.Errorf("Plan draft integrity is invalid")
		}
		state.done = true
	}
	return nil
}

func (m *Model) applySettlement(value *protocolv3.LiveGenerationSettlement) error {
	if value == nil || value.OwnerEpoch != m.liveEpoch || value.SessionId != m.sessionID || value.DraftIdentity == "" || value.GenerationId == "" || value.ProposedEntryId != value.DraftIdentity {
		return fmt.Errorf("live settlement attribution is invalid")
	}
	if err := validateLiveChannel(value.ScopeKind, value.ScopeSubagentTaskId, value.ChannelKind, value.ChannelToolCallId, value.ChannelAttemptId); err != nil {
		return err
	}
	if draft := m.live[value.DraftIdentity]; draft != nil && !draft.matches(value.GenerationId, value.ScopeKind, value.ScopeSubagentTaskId, value.ChannelKind, value.ChannelToolCallId, value.ChannelAttemptId) {
		return fmt.Errorf("live settlement generation identity conflict")
	}
	switch value.Kind {
	case protocolv3.LiveSettlementKind_LIVE_GENERATION_COMMITTED:
		if value.CommittedEntryId == "" || value.CommittedEntryId != value.DraftIdentity {
			return fmt.Errorf("live committed settlement identity is invalid")
		}
		if _, committed := m.entries[value.CommittedEntryId]; committed {
			delete(m.live, value.DraftIdentity)
		}
	case protocolv3.LiveSettlementKind_LIVE_GENERATION_ABORTED:
		if value.ReasonCode == "" {
			return fmt.Errorf("live aborted settlement reason is absent")
		}
		delete(m.live, value.DraftIdentity)
	default:
		return fmt.Errorf("live settlement kind is unknown")
	}
	return nil
}

func (m *Model) applyLiveControl(value *protocolv3.LiveControlEventProjection) error {
	switch value.Kind {
	case protocolv3.LiveControlEventKind_LIVE_INTERACTION_OPENED, protocolv3.LiveControlEventKind_LIVE_INTERACTION_REPLACED:
		if err := validateLiveInteraction(value.Interaction); err != nil {
			return err
		}
		m.currentInteraction = proto.Clone(value.Interaction).(*protocolv3.LiveInteractionView)
	case protocolv3.LiveControlEventKind_LIVE_INTERACTION_CLOSED:
		if m.currentInteraction != nil && value.ClosedInteractionId != m.currentInteraction.InteractionId {
			return fmt.Errorf("live interaction close identity conflict")
		}
		m.currentInteraction = nil
	default:
		return fmt.Errorf("live-control event kind is unknown")
	}
	return nil
}

func validateLiveInteraction(value *protocolv3.LiveInteractionView) error {
	if value == nil || value.InteractionId == "" || value.InteractionKind != "TOOL_CONFIRMATION" || value.PublicPrompt == "" || value.ExpiresAtUtc == "" || len(value.PublicOptions) != 2 || value.PublicOptions[0] != "ALLOW" || value.PublicOptions[1] != "DENY" {
		return fmt.Errorf("live interaction view is invalid")
	}
	return nil
}

func (m *Model) applyLive(event *protocolv3.LiveEventProjection) error {
	if event == nil || event.SessionId != m.sessionID || event.TurnId == "" || event.DraftIdentity == "" || event.GenerationId == "" || event.BlockId == "" || event.EventType < protocolv3.LiveEventType_TEXT_START || event.EventType > protocolv3.LiveEventType_SUBAGENT_PROGRESS {
		return fmt.Errorf("live event envelope is invalid")
	}
	if err := validateLiveChannel(event.ScopeKind, event.ScopeSubagentTaskId, event.ChannelKind, event.ChannelToolCallId, event.ChannelAttemptId); err != nil {
		return err
	}
	if (event.ChannelKind == protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT || event.ChannelKind == protocolv3.LiveChannelKind_LIVE_CHANNEL_TOOL_RESULT) != (event.ProposedEntryId == event.DraftIdentity) {
		return fmt.Errorf("live proposed-entry identity union is invalid")
	}
	if expectedLiveBlockKind(event.EventType) != event.BlockKind {
		return fmt.Errorf("live event block kind is invalid")
	}
	payload, err := validateLivePayload(event)
	if err != nil {
		return err
	}
	if payload.operational {
		return nil
	}
	if _, committed := m.entries[event.DraftIdentity]; committed {
		return nil
	}
	draft := m.live[event.DraftIdentity]
	if draft == nil {
		draft = &liveDraft{
			entryID: event.DraftIdentity, turnID: event.TurnId,
			generationID: event.GenerationId, scopeKind: event.ScopeKind,
			scopeTaskID: event.ScopeSubagentTaskId, channelKind: event.ChannelKind,
			toolCallID: event.ChannelToolCallId, attemptID: event.ChannelAttemptId,
			blocks: map[string]string{}, toolNames: map[string]string{},
			toolArguments: map[string]string{}, toolArgumentsTruncated: map[string]bool{},
			blockOrdinals: map[string]uint32{},
		}
		m.live[event.DraftIdentity] = draft
	} else if draft.turnID != event.TurnId || !draft.matches(event.GenerationId, event.ScopeKind, event.ScopeSubagentTaskId, event.ChannelKind, event.ChannelToolCallId, event.ChannelAttemptId) {
		return fmt.Errorf("live generation identity conflict")
	}
	if ordinal, exists := draft.blockOrdinals[event.BlockId]; exists {
		if ordinal != event.BlockOrdinal {
			return fmt.Errorf("live block ordinal changed")
		}
	} else {
		if int(event.BlockOrdinal) != len(draft.blockOrdinals) {
			return fmt.Errorf("live block order is not contiguous")
		}
		draft.blockOrdinals[event.BlockId] = event.BlockOrdinal
		draft.order = append(draft.order, event.BlockId)
	}
	if payload.toolStart {
		draft.toolNames[payload.blockID] = payload.toolName
		draft.blocks[payload.blockID] = renderLiveToolCall(payload.toolName, "", false)
	}
	if payload.toolArgumentsDelta != "" {
		arguments := draft.toolArguments[payload.blockID]
		truncated := draft.toolArgumentsTruncated[payload.blockID]
		if !truncated {
			arguments, truncated = boundedUTF8Prefix(
				arguments+payload.toolArgumentsDelta, maximumLiveToolArgumentBytes,
			)
			draft.toolArguments[payload.blockID] = arguments
			draft.toolArgumentsTruncated[payload.blockID] = truncated
		}
		draft.blocks[payload.blockID] = renderLiveToolCall(
			draft.toolNames[payload.blockID], arguments, truncated,
		)
	}
	if payload.appendText != "" {
		draft.blocks[payload.blockID] += publictext.Transform(payload.appendText)
	}
	if payload.finalSet {
		if payload.toolArgumentsFinal {
			arguments, truncated := boundedUTF8Prefix(
				payload.finalText, maximumLiveToolArgumentBytes,
			)
			draft.toolArguments[payload.blockID] = arguments
			draft.toolArgumentsTruncated[payload.blockID] = truncated
			draft.toolNames[payload.blockID] = payload.toolName
			draft.blocks[payload.blockID] = renderLiveToolCall(
				payload.toolName, arguments, truncated,
			)
		} else {
			draft.blocks[payload.blockID] = publictext.Transform(payload.finalText)
		}
	}
	return nil
}

type livePayloadView struct {
	blockID            string
	appendText         string
	toolName           string
	toolArgumentsDelta string
	finalText          string
	finalSet           bool
	toolStart          bool
	toolArgumentsFinal bool
	operational        bool
}

func validateLivePayload(event *protocolv3.LiveEventProjection) (livePayloadView, error) {
	if event.Payload == nil {
		return livePayloadView{}, fmt.Errorf("live event payload is absent")
	}
	view := livePayloadView{}
	switch value := event.Payload.Payload.(type) {
	case *protocolv3.LiveEventPayload_TextStart:
		if event.EventType != protocolv3.LiveEventType_TEXT_START || value.TextStart == nil {
			return view, fmt.Errorf("live text-start branch is invalid")
		}
		view.blockID = value.TextStart.BlockIdentity
	case *protocolv3.LiveEventPayload_TextDelta:
		if event.EventType != protocolv3.LiveEventType_TEXT_DELTA || value.TextDelta == nil || value.TextDelta.Delta == "" {
			return view, fmt.Errorf("live text-delta branch is invalid")
		}
		view.blockID, view.appendText = value.TextDelta.BlockIdentity, value.TextDelta.Delta
	case *protocolv3.LiveEventPayload_TextEnd:
		if event.EventType != protocolv3.LiveEventType_TEXT_END || value.TextEnd == nil || !validLiveTerminalText(value.TextEnd.FinalText, value.TextEnd.Utf8Bytes, value.TextEnd.Digest) {
			return view, fmt.Errorf("live text-end branch is invalid")
		}
		view.blockID, view.finalText, view.finalSet = value.TextEnd.BlockIdentity, value.TextEnd.FinalText, true
	case *protocolv3.LiveEventPayload_ThinkingStart:
		if event.EventType != protocolv3.LiveEventType_THINKING_START || value.ThinkingStart == nil {
			return view, fmt.Errorf("live thinking-start branch is invalid")
		}
		view.blockID = value.ThinkingStart.BlockIdentity
	case *protocolv3.LiveEventPayload_ThinkingDelta:
		if event.EventType != protocolv3.LiveEventType_THINKING_DELTA || value.ThinkingDelta == nil || value.ThinkingDelta.Delta == "" {
			return view, fmt.Errorf("live thinking-delta branch is invalid")
		}
		view.blockID, view.appendText = value.ThinkingDelta.BlockIdentity, value.ThinkingDelta.Delta
	case *protocolv3.LiveEventPayload_ThinkingEnd:
		if event.EventType != protocolv3.LiveEventType_THINKING_END || value.ThinkingEnd == nil || !validLiveTerminalText(value.ThinkingEnd.FinalText, value.ThinkingEnd.Utf8Bytes, value.ThinkingEnd.Digest) {
			return view, fmt.Errorf("live thinking-end branch is invalid")
		}
		view.blockID, view.finalText, view.finalSet = value.ThinkingEnd.BlockIdentity, value.ThinkingEnd.FinalText, true
	case *protocolv3.LiveEventPayload_DataStart:
		if event.EventType != protocolv3.LiveEventType_DATA_START || value.DataStart == nil || value.DataStart.MediaType == "" {
			return view, fmt.Errorf("live data-start branch is invalid")
		}
		view.blockID = value.DataStart.BlockIdentity
	case *protocolv3.LiveEventPayload_DataDelta:
		if event.EventType != protocolv3.LiveEventType_DATA_DELTA || value.DataDelta == nil || value.DataDelta.Data == "" {
			return view, fmt.Errorf("live data-delta branch is invalid")
		}
		view.blockID, view.appendText = value.DataDelta.BlockIdentity, value.DataDelta.Data
	case *protocolv3.LiveEventPayload_DataEnd:
		if event.EventType != protocolv3.LiveEventType_DATA_END || value.DataEnd == nil || value.DataEnd.MediaType == "" || !validLiveTerminalText(value.DataEnd.FinalData, value.DataEnd.Utf8Bytes, value.DataEnd.Digest) {
			return view, fmt.Errorf("live data-end branch is invalid")
		}
		view.blockID, view.finalText, view.finalSet = value.DataEnd.BlockIdentity, value.DataEnd.FinalData, true
	case *protocolv3.LiveEventPayload_ToolCallStart:
		if event.EventType != protocolv3.LiveEventType_TOOL_CALL_START || value.ToolCallStart == nil || value.ToolCallStart.ToolCallId == "" || value.ToolCallStart.ToolName == "" {
			return view, fmt.Errorf("live tool-call-start branch is invalid")
		}
		view.blockID, view.toolName, view.toolStart = value.ToolCallStart.BlockIdentity, value.ToolCallStart.ToolName, true
	case *protocolv3.LiveEventPayload_ToolCallDelta:
		if event.EventType != protocolv3.LiveEventType_TOOL_CALL_DELTA || value.ToolCallDelta == nil || value.ToolCallDelta.ToolCallId == "" || value.ToolCallDelta.Delta == "" {
			return view, fmt.Errorf("live tool-call-delta branch is invalid")
		}
		view.blockID, view.toolArgumentsDelta = value.ToolCallDelta.BlockIdentity, value.ToolCallDelta.Delta
	case *protocolv3.LiveEventPayload_ToolCallEnd:
		if event.EventType != protocolv3.LiveEventType_TOOL_CALL_END || value.ToolCallEnd == nil || value.ToolCallEnd.ToolCallId == "" || value.ToolCallEnd.ToolName == "" || !validLiveTerminalText(value.ToolCallEnd.ArgumentsJson, value.ToolCallEnd.Utf8Bytes, value.ToolCallEnd.Digest) {
			return view, fmt.Errorf("live tool-call-end branch is invalid")
		}
		view.blockID, view.toolName = value.ToolCallEnd.BlockIdentity, value.ToolCallEnd.ToolName
		view.finalText, view.finalSet, view.toolArgumentsFinal = value.ToolCallEnd.ArgumentsJson, true, true
	case *protocolv3.LiveEventPayload_ToolResultStart:
		if event.EventType != protocolv3.LiveEventType_TOOL_RESULT_START || value.ToolResultStart == nil || value.ToolResultStart.ToolCallId == "" || value.ToolResultStart.AttemptId == "" {
			return view, fmt.Errorf("live tool-result-start branch is invalid")
		}
		view.blockID = value.ToolResultStart.BlockIdentity
	case *protocolv3.LiveEventPayload_ToolResultDelta:
		if event.EventType != protocolv3.LiveEventType_TOOL_RESULT_DELTA || value.ToolResultDelta == nil || value.ToolResultDelta.Text == "" {
			return view, fmt.Errorf("live tool-result-delta branch is invalid")
		}
		view.blockID, view.appendText = value.ToolResultDelta.BlockIdentity, value.ToolResultDelta.Text
	case *protocolv3.LiveEventPayload_ToolResultEnd:
		if event.EventType != protocolv3.LiveEventType_TOOL_RESULT_END || value.ToolResultEnd == nil || value.ToolResultEnd.ResultState == "" || !validLiveTerminalText(value.ToolResultEnd.FinalText, value.ToolResultEnd.Utf8Bytes, value.ToolResultEnd.Digest) {
			return view, fmt.Errorf("live tool-result-end branch is invalid")
		}
		view.blockID, view.finalText, view.finalSet = value.ToolResultEnd.BlockIdentity, value.ToolResultEnd.FinalText, true
	case *protocolv3.LiveEventPayload_InteractionOpened:
		view.operational = event.EventType == protocolv3.LiveEventType_INTERACTION_OPENED && value.InteractionOpened != nil && value.InteractionOpened.InteractionId != "" && value.InteractionOpened.InteractionKind != "" && value.InteractionOpened.PublicPrompt != "" && value.InteractionOpened.ExpiresAtUtc != ""
	case *protocolv3.LiveEventPayload_InteractionReplaced:
		view.operational = event.EventType == protocolv3.LiveEventType_INTERACTION_REPLACED && value.InteractionReplaced != nil && value.InteractionReplaced.ReplacedInteractionId != "" && value.InteractionReplaced.InteractionId != "" && value.InteractionReplaced.InteractionKind != "" && value.InteractionReplaced.PublicPrompt != "" && value.InteractionReplaced.ExpiresAtUtc != ""
	case *protocolv3.LiveEventPayload_InteractionClosed:
		view.operational = event.EventType == protocolv3.LiveEventType_INTERACTION_CLOSED && value.InteractionClosed != nil && value.InteractionClosed.InteractionId != "" && value.InteractionClosed.Reason != ""
	case *protocolv3.LiveEventPayload_TerminalProcessCompleted:
		view.operational = event.EventType == protocolv3.LiveEventType_TERMINAL_PROCESS_COMPLETED && value.TerminalProcessCompleted != nil && value.TerminalProcessCompleted.ProcessId != "" && value.TerminalProcessCompleted.Status != "" && validLiveDigest(value.TerminalProcessCompleted.OutputDigest)
	case *protocolv3.LiveEventPayload_TerminalMonitorOpened:
		view.operational = event.EventType == protocolv3.LiveEventType_TERMINAL_MONITOR_OPENED && value.TerminalMonitorOpened != nil && value.TerminalMonitorOpened.MonitorId != "" && value.TerminalMonitorOpened.ProcessId != ""
	case *protocolv3.LiveEventPayload_TerminalMonitorObservation:
		view.operational = event.EventType == protocolv3.LiveEventType_TERMINAL_MONITOR_OBSERVATION && value.TerminalMonitorObservation != nil && value.TerminalMonitorObservation.MonitorId != "" && value.TerminalMonitorObservation.ProcessId != "" && value.TerminalMonitorObservation.ObservationKind != "" && uint64(len([]byte(value.TerminalMonitorObservation.PublicPreview))) <= value.TerminalMonitorObservation.CompleteUtf8Bytes && validLiveDigest(value.TerminalMonitorObservation.CompleteDigest)
	case *protocolv3.LiveEventPayload_TerminalMonitorClosed:
		view.operational = event.EventType == protocolv3.LiveEventType_TERMINAL_MONITOR_CLOSED && value.TerminalMonitorClosed != nil && value.TerminalMonitorClosed.MonitorId != "" && value.TerminalMonitorClosed.ProcessId != "" && value.TerminalMonitorClosed.Reason != ""
	case *protocolv3.LiveEventPayload_SubagentProgress:
		view.operational = event.EventType == protocolv3.LiveEventType_SUBAGENT_PROGRESS && value.SubagentProgress != nil && value.SubagentProgress.TaskId != "" && value.SubagentProgress.Status != "" && validLiveTerminalText(value.SubagentProgress.PublicSummary, value.SubagentProgress.SummaryUtf8Bytes, value.SubagentProgress.SummaryDigest)
	default:
		return view, fmt.Errorf("live event payload branch is unknown")
	}
	if view.operational {
		return view, nil
	}
	if view.blockID == "" || view.blockID != event.BlockId {
		return view, fmt.Errorf("live block payload identity conflict")
	}
	return view, nil
}

func validLiveTerminalText(value string, size uint64, fingerprint string) bool {
	return uint64(len([]byte(value))) == size && digest([]byte(value)) == fingerprint
}

func validLiveDigest(value string) bool {
	return len(value) == len("sha256:")+64 && strings.HasPrefix(value, "sha256:")
}

func (d *liveDraft) matches(generationID string, scopeKind protocolv3.ConversationScopeKind, scopeTaskID string, channelKind protocolv3.LiveChannelKind, toolCallID, attemptID string) bool {
	return d.generationID == generationID && d.scopeKind == scopeKind && d.scopeTaskID == scopeTaskID && d.channelKind == channelKind && d.toolCallID == toolCallID && d.attemptID == attemptID
}

func validateLiveChannel(scopeKind protocolv3.ConversationScopeKind, scopeTaskID string, channelKind protocolv3.LiveChannelKind, toolCallID, attemptID string) error {
	if (scopeKind == protocolv3.ConversationScopeKind_ROOT) != (scopeTaskID == "") {
		return fmt.Errorf("live conversation scope union is invalid")
	}
	if scopeKind != protocolv3.ConversationScopeKind_ROOT && scopeKind != protocolv3.ConversationScopeKind_SUBAGENT_TASK {
		return fmt.Errorf("live conversation scope is unknown")
	}
	switch channelKind {
	case protocolv3.LiveChannelKind_LIVE_CHANNEL_MODEL_OUTPUT:
		if toolCallID != "" || attemptID != "" {
			return fmt.Errorf("model-output live channel carries tool attribution")
		}
	case protocolv3.LiveChannelKind_LIVE_CHANNEL_TOOL_RESULT:
		if toolCallID == "" || attemptID == "" {
			return fmt.Errorf("tool-result live channel lacks exact attribution")
		}
	case protocolv3.LiveChannelKind_LIVE_CHANNEL_TERMINAL_EXTENSION, protocolv3.LiveChannelKind_LIVE_CHANNEL_SUBAGENT_EXTENSION:
		if toolCallID != "" || attemptID != "" {
			return fmt.Errorf("extension live channel carries tool attribution")
		}
	default:
		return fmt.Errorf("live channel kind is unknown")
	}
	return nil
}

func expectedLiveBlockKind(eventType protocolv3.LiveEventType) protocolv3.LiveBlockKind {
	switch eventType {
	case protocolv3.LiveEventType_TEXT_START, protocolv3.LiveEventType_TEXT_DELTA, protocolv3.LiveEventType_TEXT_END:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_TEXT
	case protocolv3.LiveEventType_THINKING_START, protocolv3.LiveEventType_THINKING_DELTA, protocolv3.LiveEventType_THINKING_END:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_THINKING
	case protocolv3.LiveEventType_DATA_START, protocolv3.LiveEventType_DATA_DELTA, protocolv3.LiveEventType_DATA_END:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_DATA
	case protocolv3.LiveEventType_TOOL_CALL_START, protocolv3.LiveEventType_TOOL_CALL_DELTA, protocolv3.LiveEventType_TOOL_CALL_END:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_TOOL_CALL
	case protocolv3.LiveEventType_TOOL_RESULT_START, protocolv3.LiveEventType_TOOL_RESULT_DELTA, protocolv3.LiveEventType_TOOL_RESULT_END:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_TOOL_RESULT
	default:
		return protocolv3.LiveBlockKind_LIVE_BLOCK_OPERATIONAL
	}
}

func (m *Model) installEntry(entry *protocolv3.CanonicalEntry) error {
	if entry == nil || entry.EntryId == "" || entry.EntrySequence == 0 || entry.EntryKind == protocolv3.EntryKind_ENTRY_KIND_UNSPECIFIED {
		return fmt.Errorf("canonical entry is invalid")
	}
	if err := validateContentReference(entry.Content); err != nil {
		return err
	}
	if entry.ScopeKind == protocolv3.ConversationScopeKind_ROOT {
		if entry.ScopeSubagentTaskId != "" {
			return fmt.Errorf("ROOT entry carries a task scope")
		}
	} else if entry.ScopeKind == protocolv3.ConversationScopeKind_SUBAGENT_TASK {
		if entry.ScopeSubagentTaskId == "" {
			return fmt.Errorf("task entry lacks a task identity")
		}
	} else {
		return fmt.Errorf("canonical entry scope is unknown")
	}
	for ordinal, block := range entry.Blocks {
		if block == nil || block.BlockId == "" || int(block.Ordinal) != ordinal {
			return fmt.Errorf("canonical assistant block order is invalid")
		}
		switch block.BlockKind {
		case "TEXT", "DATA":
			if block.ToolCallId != "" || block.ToolName != "" || block.ToolArgumentsSize != 0 || block.Content == nil {
				return fmt.Errorf("canonical content block union is invalid")
			}
			if err := validateContentReference(block.Content); err != nil {
				return err
			}
		case "TOOL_CALL":
			if block.ToolCallId == "" || block.ToolName == "" || block.Content != nil || len(block.ToolArgumentsDigest) != 71 || !strings.HasPrefix(block.ToolArgumentsDigest, "sha256:") || block.ToolArgumentsSize == 0 || uint64(len(block.ToolArgumentsPreview)) > block.ToolArgumentsSize {
				return fmt.Errorf("canonical tool-call block union is invalid")
			}
			if block.ToolArgumentsTruncated != (uint64(len(block.ToolArgumentsPreview)) < block.ToolArgumentsSize) {
				return fmt.Errorf("canonical tool argument truncation is invalid")
			}
			if !block.ToolArgumentsTruncated && digest(block.ToolArgumentsPreview) != block.ToolArgumentsDigest {
				return fmt.Errorf("canonical tool argument digest is invalid")
			}
		default:
			return fmt.Errorf("canonical assistant block kind is unknown")
		}
	}
	if current := m.entries[entry.EntryId]; current != nil && !proto.Equal(current, entry) {
		return fmt.Errorf("canonical entry identity conflict")
	}
	if draft := m.live[entry.EntryId]; draft != nil {
		if draft.turnID != entry.TurnId || draft.scopeKind != entry.ScopeKind || draft.scopeTaskID != entry.ScopeSubagentTaskId {
			return fmt.Errorf("canonical entry conflicts with its provisional live generation")
		}
	}
	if m.entries[entry.EntryId] == nil {
		entrySize := proto.Size(entry)
		if len(m.entries) >= maximumCachedEntries || m.entryBytes+entrySize > maximumCachedEntryBytes {
			return fmt.Errorf("canonical entry cache bound exceeded")
		}
		m.order = append(m.order, entry.EntryId)
		m.entryBytes += entrySize
	}
	m.entries[entry.EntryId] = proto.Clone(entry).(*protocolv3.CanonicalEntry)
	delete(m.live, entry.EntryId)
	sort.SliceStable(m.order, func(i, j int) bool { return m.entries[m.order[i]].EntrySequence < m.entries[m.order[j]].EntrySequence })
	return nil
}

func (m *Model) applyHistory(response *protocolv3.HistoryPageResponse) error {
	for _, entry := range response.Entries {
		if err := m.installEntry(entry); err != nil {
			return err
		}
	}
	if response.HasMore && response.OlderHistoryCursor == nil {
		return fmt.Errorf("history continuation cursor is missing")
	}
	m.older = cloneCursor(response.OlderHistoryCursor)
	return nil
}

func (m *Model) applyContent(message contentMsg) error {
	chunk := message.value
	key := contentKey(message.entryID, message.blockID)
	reference := m.contentReference(message.entryID, message.blockID)
	if reference == nil || reference.Kind != protocolv3.ContentKind_CANONICAL_BLOB || reference.Digest != chunk.Digest || reference.Size != chunk.CompleteSize {
		return fmt.Errorf("content hydration does not match canonical reference")
	}
	state := m.content[key]
	if state == nil {
		state = &contentState{entryID: message.entryID, blockID: message.blockID, digest: chunk.Digest, total: chunk.CompleteSize}
		m.content[key] = state
	}
	if state.digest != chunk.Digest || state.total != chunk.CompleteSize || chunk.OffsetBytes != uint64(len(state.value)) || uint64(len(state.value)+len(chunk.Content)) > min(chunk.CompleteSize, uint64(maximumHydratedBytes)) {
		return fmt.Errorf("content hydration join failed")
	}
	remaining := maximumCachedBytes - m.entryBytes - m.contentBytes()
	if remaining <= 0 || len(chunk.Content) > remaining {
		state.value = nil
		state.done, state.unavailable = true, true
		return nil
	}
	state.value = append(state.value, chunk.Content...)
	if chunk.Complete {
		if uint64(len(state.value)) != state.total || digest(state.value) != state.digest {
			return &contentIntegrityError{reason: "content hydration digest is invalid"}
		}
		state.done, state.verified = true, true
	} else if len(state.value) == maximumHydratedBytes {
		return &contentIntegrityError{reason: "canonical blob exceeded its hard bound"}
	}
	return nil
}

func (m Model) render() string {
	height, width := max(m.height, 1), max(m.width, 1)
	if height == 1 {
		return ansi.Truncate(m.phaseText()+" · Ctrl-D", width, "")
	}
	header := ansi.Truncate("Pulsara  "+m.phaseText()+" · "+permissionModeLabel(m.permissionMode)+m.statusText(), width, "")
	composerRows := 2
	if height < 4 {
		composerRows = 1
	}
	bodyHeight := max(0, height-1-composerRows)
	rows := m.transcriptRows(width)
	start := max(0, len(rows)-bodyHeight-m.scroll)
	end := min(len(rows), start+bodyHeight)
	visible := append([]string(nil), rows[start:end]...)
	for len(visible) < bodyHeight {
		visible = append(visible, "")
	}
	result := []string{pad(header, width)}
	for _, row := range visible {
		result = append(result, pad(ansi.Truncate(row, width, ""), width))
	}
	if composerRows == 2 {
		prompt := "> " + string(m.draft[:min(m.cursor, len(m.draft))]) + "▏" + string(m.draft[min(m.cursor, len(m.draft)):])
		if interaction := m.openPlanInteraction(); interaction != nil {
			switch interaction.Kind {
			case "QUESTION":
				if m.planQuestion == nil {
					prompt = "> Loading Plan question…"
				} else {
					prompt = "> " + publictext.Transform(m.planQuestion.Question)
				}
			case "DRAFT_REVIEW":
				if m.planDraft == nil || !m.planDraft.done {
					prompt = "> Loading Plan draft…"
				} else {
					preview := publictext.Transform(string(m.planDraft.value))
					prompt = "> Review Plan: " + preview
				}
			}
		}
		if m.currentInteraction != nil {
			prompt = "> " + publictext.Transform(m.currentInteraction.PublicPrompt)
		}
		if m.phase != phaseReady {
			prompt = "> " + m.phaseText()
		}
		result = append(result, pad(ansi.Truncate(prompt, width, ""), width))
	}
	footer := "Enter send · Alt+Enter newline · Ctrl-C stop · PgUp transcript · ↑↓ prompts · Ctrl-D detach"
	if m.activePlanWorkflow() == nil {
		footer = "Ctrl-P permission · Ctrl-L Plan · Enter send · Ctrl-D detach"
	} else {
		footer = "Plan read-only · Ctrl-X cancel · Ctrl-F force · Ctrl-D detach"
	}
	if interaction := m.openPlanInteraction(); interaction != nil {
		if interaction.Kind == "QUESTION" {
			footer = "1-3 option · Enter free text · Ctrl-F force · Ctrl-D detach"
		} else {
			footer = "a approve · r revise · c cancel · Ctrl-F force · Ctrl-D detach"
		}
	}
	if m.currentInteraction != nil {
		footer = "y/Enter allow · n/Esc deny · Ctrl-D detach"
	}
	if width < 70 {
		footer = "Enter send · PgUp transcript · Ctrl-D detach"
	}
	if width < 38 {
		footer = "Enter · PgUp · Ctrl-D"
	}
	result = append(result, pad(ansi.Truncate(footer, width, ""), width))
	return strings.Join(result[:min(len(result), height)], "\n")
}

func (m Model) transcriptRows(width int) []string {
	rows := []string{}
	for _, id := range m.order {
		entry := m.entries[id]
		if entry.ScopeKind != protocolv3.ConversationScopeKind_ROOT {
			continue
		}
		rows = append(rows, entryLabel(entry))
		for _, value := range strings.Split(m.entryText(entry), "\n") {
			rows = append(rows, wrap(value, width)...)
		}
	}
	keys := make([]string, 0, len(m.live))
	for key := range m.live {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		draft := m.live[key]
		if draft.scopeKind != protocolv3.ConversationScopeKind_ROOT {
			continue
		}
		rows = append(rows, "assistant · streaming")
		for _, block := range draft.order {
			for _, value := range strings.Split(draft.blocks[block], "\n") {
				rows = append(rows, wrap(value, width)...)
			}
		}
	}
	if len(rows) == 0 {
		rows = append(rows, "No conversation entries yet.")
	}
	return rows
}

func (m Model) entryText(entry *protocolv3.CanonicalEntry) string {
	parts := []string{}
	if entry.EntryKind != protocolv3.EntryKind_ASSISTANT_MESSAGE && entry.EntryKind != protocolv3.EntryKind_ASSISTANT_TOOL_REQUEST {
		if value := m.contentText(entry.EntryId, "", entry.Content); value != "" {
			parts = append(parts, value)
		}
	}
	for _, block := range entry.Blocks {
		switch block.BlockKind {
		case "TOOL_CALL":
			preview := publictext.Transform(string(block.ToolArgumentsPreview))
			if block.ToolArgumentsTruncated {
				preview += "… [arguments truncated]"
			}
			parts = append(parts, fmt.Sprintf("%s(%s)", block.ToolName, preview))
		default:
			if value := m.contentText(entry.EntryId, block.BlockId, block.Content); value != "" {
				parts = append(parts, value)
			}
		}
	}
	return strings.Join(parts, "\n")
}

func (m Model) contentText(entryID, blockID string, reference *protocolv3.CanonicalContentReference) string {
	if reference == nil {
		return ""
	}
	if reference.Kind == protocolv3.ContentKind_INLINE {
		return publictext.Transform(string(reference.InlineContent))
	}
	if state := m.content[contentKey(entryID, blockID)]; state != nil && state.done {
		if state.unavailable {
			return "[canonical content unavailable]"
		}
		if state.truncated {
			return "[canonical content unavailable: client cache capacity]"
		}
		if !state.verified {
			return "[canonical content integrity unavailable]"
		}
		return publictext.Transform(string(state.value))
	}
	return fmt.Sprintf("[content %s · %d bytes]", reference.Digest, reference.Size)
}

func (m Model) contentReference(entryID, blockID string) *protocolv3.CanonicalContentReference {
	entry := m.entries[entryID]
	if entry == nil {
		return nil
	}
	if blockID == "" {
		return entry.Content
	}
	for _, block := range entry.Blocks {
		if block.BlockId == blockID {
			return block.Content
		}
	}
	return nil
}

func (m *Model) nextContentCommand() tea.Cmd {
	if m.contentLoading {
		return nil
	}
	for _, id := range m.order {
		entry := m.entries[id]
		if entry.ScopeKind != protocolv3.ConversationScopeKind_ROOT {
			continue
		}
		if command := m.contentCommandFor(id, "", entry.Content); command != nil {
			m.contentLoading = true
			return command
		}
		for _, block := range entry.Blocks {
			if command := m.contentCommandFor(id, block.BlockId, block.Content); command != nil {
				m.contentLoading = true
				return command
			}
		}
	}
	return nil
}

func (m Model) contentCommandFor(entryID, blockID string, reference *protocolv3.CanonicalContentReference) tea.Cmd {
	if reference == nil || reference.Kind != protocolv3.ContentKind_CANONICAL_BLOB {
		return nil
	}
	state := m.content[contentKey(entryID, blockID)]
	if state != nil && state.done {
		return nil
	}
	offset := uint64(0)
	if state != nil {
		offset = uint64(len(state.value))
	}
	if offset >= maximumHydratedBytes {
		return nil
	}
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.ReadContent(ctx, entryID, blockID, offset)
		return contentMsg{value: value, err: err, entryID: entryID, blockID: blockID}
	}
}

func (m Model) helloCommand(generation uint64) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		value, err := m.service.Hello(ctx)
		return helloMsg{value: value, err: err, generation: generation}
	}
}
func (m Model) snapshotCommand() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.Snapshot(ctx, maximumResidentEntries, maximumControlItems)
		return snapshotMsg{value: value, err: err}
	}
}
func (m Model) liveControlSnapshotCommand() tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		value, err := m.service.LiveControlSnapshot(ctx)
		return liveControlSnapshotMsg{value: value, err: err}
	}
}
func (m Model) observeCommand() tea.Cmd {
	event, epoch, live := m.event, m.liveEpoch, m.liveRevision
	controlEpoch, controlRevision := m.controlEpoch, m.controlLiveRevision
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.Observe(ctx, event, epoch, live, controlEpoch, controlRevision)
		return observationMsg{value: value, err: err}
	}
}

func (m Model) heartbeatAfter() tea.Cmd {
	interval, generation := m.heartbeatInterval, m.reconnectGeneration
	if interval <= 0 {
		interval = 10 * time.Second
	}
	return tea.Tick(interval, func(time.Time) tea.Msg {
		return heartbeatDueMsg{generation: generation}
	})
}

func (m Model) heartbeatCommand(generation uint64) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		value, err := m.service.Heartbeat(ctx)
		return heartbeatMsg{value: value, err: err, generation: generation}
	}
}
func (m Model) historyCommand() tea.Cmd {
	cursor := cloneCursor(m.older)
	maximumEntries := uint32(min(maximumHistoryPage, m.historyCapacity()))
	maximumBytes := uint32(min(maximumHistoryBytes, max(1, maximumCachedBytes-m.entryBytes-m.contentBytes())))
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.History(ctx, cursor, maximumEntries, maximumBytes)
		return historyMsg{value: value, err: err, maximumEntries: maximumEntries, maximumBytes: maximumBytes}
	}
}
func (m Model) commandCommand(command pendingCommand, kind protocolv3.CommandKind) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		var value *protocolv3.CommandOutcome
		var err error
		if kind == protocolv3.CommandKind_SUBMIT_PROMPT || kind == protocolv3.CommandKind_ENTER_PLAN || kind == protocolv3.CommandKind_CANCEL_PLAN || kind == protocolv3.CommandKind_FORCE_EXIT_PLAN {
			service, ok := m.service.(planCommandService)
			if !ok {
				return commandMsg{err: errors.New("Protocol v3 Plan/permission client is unavailable"), frozen: command}
			}
			requested := protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED
			if kind == protocolv3.CommandKind_SUBMIT_PROMPT || kind == protocolv3.CommandKind_ENTER_PLAN {
				requested = command.requestedMode
			}
			value, err = service.CommandWithPlanFields(ctx, command.id, kind, command.text, command.target, requested, command.planWorkflowID, command.planWorkflowRevision)
		} else {
			value, err = m.service.Command(ctx, command.id, kind, command.text, command.target)
		}
		return commandMsg{value: value, err: err, frozen: command}
	}
}

func (m Model) resolvePlanCommand(command pendingCommand, answer *protocolv3.PlanQuestionAnswer, decision protocolv3.PlanDraftDecision, feedback *string) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		service, ok := m.service.(planResolutionService)
		if !ok {
			return planResolutionMsg{err: errors.New("Protocol v3 Plan client is unavailable"), frozen: command}
		}
		var value *protocolv3.ResolvePlanInteractionResponse
		var err error
		if answer != nil {
			value, err = service.ResolvePlanQuestion(ctx, command.id, command.planWorkflowID, command.interactionID, command.writerGeneration, command.planWorkflowRevision, answer)
		} else {
			value, err = service.ResolvePlanDraft(ctx, command.id, command.planWorkflowID, command.interactionID, command.writerGeneration, command.planWorkflowRevision, decision, feedback)
		}
		return planResolutionMsg{value: value, err: err, frozen: command}
	}
}

func (m Model) nextPlanContentCommand() tea.Cmd {
	interaction := m.openPlanInteraction()
	if interaction == nil {
		return nil
	}
	service, ok := m.service.(planContentService)
	if !ok {
		return nil
	}
	if interaction.Kind == "QUESTION" && (m.planQuestion == nil || m.planQuestion.InteractionId != interaction.InteractionId) {
		interactionID := interaction.InteractionId
		return func() tea.Msg {
			ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
			defer cancel()
			value, err := service.ReadPlanQuestion(ctx, interactionID)
			return planQuestionMsg{value: value, err: err, interactionID: interactionID}
		}
	}
	if interaction.Kind == "DRAFT_REVIEW" && m.planDraft != nil && !m.planDraft.done && !m.planDraft.loading {
		m.planDraft.loading = true
		interactionID, digest, offset := m.planDraft.interactionID, m.planDraft.digest, uint64(len(m.planDraft.value))
		return func() tea.Msg {
			ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
			defer cancel()
			value, err := service.ReadPlanDraft(ctx, interactionID, digest, offset, 64<<10)
			return planDraftMsg{value: value, err: err, interactionID: interactionID}
		}
	}
	return nil
}
func (m Model) resolveInteractionCommand(command pendingCommand) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.ResolveInteraction(
			ctx, command.id, command.writerGeneration, command.controlEpoch,
			command.controlRevision, command.interactionID, command.interactionDecision,
		)
		return commandMsg{value: value, err: err, frozen: command}
	}
}
func (m Model) queryCommand(commandID string) tea.Cmd {
	return func() tea.Msg {
		ctx, cancel := context.WithTimeout(context.Background(), 12*time.Second)
		defer cancel()
		value, err := m.service.QueryCommand(ctx, commandID)
		return queryMsg{value: value, err: err, commandID: commandID}
	}
}

func (m Model) scheduleReconnect(err error) (tea.Model, tea.Cmd) {
	m.service.ResetConnection()
	m.phase = phaseReconnecting
	m.preserveLiveOnSnapshot = false
	m.notice = "Reconnecting"
	m.reconnectGeneration++
	generation := m.reconnectGeneration
	_ = err
	return m, tea.Tick(250*time.Millisecond, func(time.Time) tea.Msg { return reconnectDueMsg{generation: generation} })
}
func (m Model) fail(err error) (tea.Model, tea.Cmd) {
	m.phase = phaseFatal
	m.failure = publictext.Transform(err.Error())
	return m, nil
}
func queryAfter(id string) tea.Cmd {
	return tea.Tick(500*time.Millisecond, func(time.Time) tea.Msg { return queryDueMsg{commandID: id} })
}

func (m Model) canEdit() bool {
	return m.phase == phaseReady && m.height >= 4 && m.role == protocolv3.AttachmentRole_ATTACHMENT_ROLE_CONTROLLER && m.currentInteraction == nil
}
func (m Model) canSubmit() bool {
	return m.canEdit() && m.pending == nil && len(m.trackedPrompts) < maximumTrackedPromptCommands
}
func (m Model) transcriptHeight() int { return max(1, m.height-3) }

func isPromptCommand(kind protocolv3.CommandKind) bool {
	return kind == protocolv3.CommandKind_SUBMIT_PROMPT || kind == protocolv3.CommandKind_STEER_ACTIVE_TURN
}

func promptIngressAccepted(publicCode string) bool {
	return publicCode == "PROMPT_CONSUMED" || publicCode == "TURN_RUNNING"
}

func (m Model) isCommandAwaitingQuery(commandID string) bool {
	if m.pending != nil && m.pending.id == commandID {
		return true
	}
	_, exists := m.trackedPrompts[commandID]
	return exists
}

func (m Model) commandAwaitingQuery(commandID string) (pendingCommand, bool, bool) {
	if m.pending != nil && m.pending.id == commandID {
		return *m.pending, true, true
	}
	command, exists := m.trackedPrompts[commandID]
	return command, false, exists
}

func (m *Model) clearAwaitingCommand(commandID string, isPending bool) {
	if isPending {
		if m.pending != nil && m.pending.id == commandID {
			m.pending = nil
		}
		return
	}
	delete(m.trackedPrompts, commandID)
}

func (m Model) trackedPromptIDs() []string {
	ids := make([]string, 0, len(m.trackedPrompts))
	for commandID := range m.trackedPrompts {
		ids = append(ids, commandID)
	}
	sort.Strings(ids)
	return ids
}

func (m Model) activeRootTurnID() (string, bool) {
	if m.control == nil {
		return "", false
	}
	for _, turn := range m.control.ActiveTurns {
		if turn != nil && turn.ScopeKind == protocolv3.ConversationScopeKind_ROOT && turn.Status == "RUNNING" {
			return turn.TurnId, true
		}
	}
	return "", false
}

func (m Model) phaseText() string {
	switch m.phase {
	case phaseConnecting:
		return "connecting"
	case phaseLoading:
		return "loading"
	case phaseReady:
		return "ready"
	case phaseReconnecting:
		return "reconnecting"
	case phaseFatal:
		return "fatal"
	}
	return "unknown"
}
func (m Model) statusText() string {
	if m.failure != "" {
		return " · " + m.failure
	}
	if m.notice != "" {
		return " · " + m.notice
	}
	if m.pending != nil {
		return " · command pending"
	}
	return fmt.Sprintf(" · revision %d · queue %d", m.event, queueCount(m.control))
}

func (m *Model) insertText(value string) {
	if !utf8.ValidString(value) || len([]byte(string(m.draft)+value)) > maximumDraftBytes {
		m.notice = "Draft size limit reached"
		return
	}
	runes := []rune(value)
	m.draft = append(m.draft[:m.cursor], append(runes, m.draft[m.cursor:]...)...)
	m.cursor += len(runes)
	m.exitHistoryTraversal()
}
func (m *Model) restoreRejectedDraft(value string) {
	if len(m.draft) == 0 {
		m.draft = []rune(value)
		m.cursor = len(m.draft)
	}
}
func (m *Model) acceptPromptHistory(value string) {
	if value == "" || len([]byte(value)) > 32<<10 {
		return
	}
	if len(m.promptHistory) == 0 || m.promptHistory[len(m.promptHistory)-1] != value {
		m.promptHistory = append(m.promptHistory, value)
		if len(m.promptHistory) > 100 {
			m.promptHistory = m.promptHistory[len(m.promptHistory)-100:]
		}
	}
}
func (m *Model) previousPrompt() {
	if len(m.promptHistory) == 0 {
		return
	}
	if m.historyIndex < 0 {
		m.historyScratch = string(m.draft)
		m.historyIndex = len(m.promptHistory) - 1
	} else if m.historyIndex > 0 {
		m.historyIndex--
	}
	m.draft = []rune(m.promptHistory[m.historyIndex])
	m.cursor = len(m.draft)
}
func (m *Model) nextPrompt() {
	if m.historyIndex < 0 {
		return
	}
	if m.historyIndex < len(m.promptHistory)-1 {
		m.historyIndex++
		m.draft = []rune(m.promptHistory[m.historyIndex])
	} else {
		m.historyIndex = -1
		m.draft = []rune(m.historyScratch)
		m.historyScratch = ""
	}
	m.cursor = len(m.draft)
}
func (m *Model) exitHistoryTraversal() { m.historyIndex = -1; m.historyScratch = "" }

func entryLabel(entry *protocolv3.CanonicalEntry) string {
	switch entry.EntryKind {
	case protocolv3.EntryKind_USER_MESSAGE, protocolv3.EntryKind_USER_STEER:
		return "user"
	case protocolv3.EntryKind_ASSISTANT_MESSAGE, protocolv3.EntryKind_ASSISTANT_TOOL_REQUEST:
		return "assistant"
	case protocolv3.EntryKind_TOOL_RESULT:
		return "tool"
	case protocolv3.EntryKind_TERMINAL_OBSERVATION:
		return "terminal"
	}
	return "entry"
}
func cloneCursor(value *protocolv3.HistoryCursor) *protocolv3.HistoryCursor {
	if value == nil || value.SessionId == "" {
		return nil
	}
	return proto.Clone(value).(*protocolv3.HistoryCursor)
}
func validateContentReference(value *protocolv3.CanonicalContentReference) error {
	if value == nil || value.Digest == "" || value.MediaType == "" || value.Codec == "" {
		return fmt.Errorf("canonical content reference is incomplete")
	}
	switch value.Kind {
	case protocolv3.ContentKind_INLINE:
		if uint64(len(value.InlineContent)) != value.Size || digest(value.InlineContent) != value.Digest {
			return fmt.Errorf("canonical inline content integrity is invalid")
		}
	case protocolv3.ContentKind_CANONICAL_BLOB:
		if len(value.InlineContent) != 0 || value.Size > maximumHydratedBytes || len(value.Digest) != 71 || !strings.HasPrefix(value.Digest, "sha256:") {
			return fmt.Errorf("canonical blob reference is invalid")
		}
	default:
		return fmt.Errorf("canonical content kind is unknown")
	}
	return nil
}
func digest(value []byte) string {
	sum := sha256.Sum256(value)
	return "sha256:" + hex.EncodeToString(sum[:])
}
func planDraftDigest(value []byte) string {
	prefix := []byte("pulsara:plan-draft-utf8:v1\x00")
	length := make([]byte, 8)
	for index := 7; index >= 0; index-- {
		length[index] = byte(len(value))
		valueLength := len(value) >> (8 * (7 - index))
		length[index] = byte(valueLength)
	}
	payload := append(prefix, length...)
	payload = append(payload, value...)
	return digest(payload)
}

func (m Model) activePlanWorkflow() *protocolv3.PlanWorkflowControl {
	if m.control == nil || m.control.ActivePlanWorkflow == nil {
		return nil
	}
	return m.control.ActivePlanWorkflow
}

func (m Model) openPlanInteraction() *protocolv3.PlanInteractionControl {
	if m.control == nil || m.control.OpenPlanInteraction == nil {
		return nil
	}
	return m.control.OpenPlanInteraction
}

func (m *Model) syncPlanControl() {
	workflow, interaction := m.activePlanWorkflow(), m.openPlanInteraction()
	if workflow != nil && workflow.ResumePermissionMode != protocolv3.PermissionMode_PERMISSION_MODE_UNSPECIFIED {
		m.permissionMode = workflow.ResumePermissionMode
	}
	if interaction == nil {
		m.planQuestion = nil
		m.planDraft = nil
		return
	}
	if interaction.Kind == "QUESTION" {
		if m.planQuestion != nil && m.planQuestion.InteractionId != interaction.InteractionId {
			m.planQuestion = nil
		}
		m.planDraft = nil
		return
	}
	m.planQuestion = nil
	if m.planDraft == nil || m.planDraft.interactionID != interaction.InteractionId || m.planDraft.digest != interaction.DraftUtf8Digest || m.planDraft.total != interaction.DraftUtf8Size {
		m.planDraft = &planDraftState{interactionID: interaction.InteractionId, digest: interaction.DraftUtf8Digest, total: interaction.DraftUtf8Size}
	}
}

func nextPermissionMode(value protocolv3.PermissionMode) protocolv3.PermissionMode {
	switch value {
	case protocolv3.PermissionMode_PERMISSION_MODE_ACCEPT_EDITS:
		return protocolv3.PermissionMode_PERMISSION_MODE_READ_ONLY
	case protocolv3.PermissionMode_PERMISSION_MODE_READ_ONLY:
		return protocolv3.PermissionMode_PERMISSION_MODE_ASK_PERMISSIONS
	case protocolv3.PermissionMode_PERMISSION_MODE_ASK_PERMISSIONS:
		return protocolv3.PermissionMode_PERMISSION_MODE_BYPASS_PERMISSIONS
	default:
		return protocolv3.PermissionMode_PERMISSION_MODE_ACCEPT_EDITS
	}
}

func permissionModeLabel(value protocolv3.PermissionMode) string {
	switch value {
	case protocolv3.PermissionMode_PERMISSION_MODE_ACCEPT_EDITS:
		return "accept-edits"
	case protocolv3.PermissionMode_PERMISSION_MODE_READ_ONLY:
		return "read-only"
	case protocolv3.PermissionMode_PERMISSION_MODE_ASK_PERMISSIONS:
		return "ask-permissions"
	case protocolv3.PermissionMode_PERMISSION_MODE_BYPASS_PERMISSIONS:
		return "bypass-permissions"
	default:
		return "unknown"
	}
}
func canonicalSnapshotFingerprint(value *protocolv3.CanonicalSessionSnapshot) string {
	clone := proto.Clone(value).(*protocolv3.CanonicalSessionSnapshot)
	clone.SnapshotFingerprint = ""
	payload, err := proto.MarshalOptions{Deterministic: true}.Marshal(clone)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(append([]byte("terminal-canonical-snapshot:v3\x00"), payload...))
	return "sha256:" + hex.EncodeToString(sum[:])
}
func contentKey(entryID, blockID string) string { return entryID + "\x00" + blockID }
func (m Model) historyCapacity() int {
	return max(0, maximumCachedEntries-len(m.entries))
}
func (m Model) contentBytes() int {
	total := 0
	for _, state := range m.content {
		total += len(state.value)
	}
	return total
}
func queueCount(control *protocolv3.CanonicalControl) uint64 {
	if control == nil {
		return 0
	}
	return control.PromptQueueTotalCount
}
func newID(prefix string) string {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		panic(err)
	}
	return prefix + ":" + hex.EncodeToString(value[:])
}
func wrap(value string, width int) []string {
	value = publictext.Transform(value)
	if value == "" {
		return []string{""}
	}
	wrapped := ansi.Hardwrap(value, max(width, 1), true)
	return strings.Split(wrapped, "\n")
}

func renderLiveToolCall(toolName, arguments string, truncated bool) string {
	preview := publictext.Transform(arguments)
	if truncated {
		preview += liveToolArgumentsTruncated
	}
	return fmt.Sprintf("%s(%s)", publictext.Transform(toolName), preview)
}

func boundedUTF8Prefix(value string, maximumBytes int) (string, bool) {
	if len(value) <= maximumBytes {
		return value, false
	}
	limit := maximumBytes - len(liveToolArgumentsTruncated)
	if limit < 0 {
		limit = 0
	}
	for limit > 0 && !utf8.RuneStart(value[limit]) {
		limit--
	}
	return value[:limit], true
}

func isCanonicalContentUnavailable(err error) bool {
	var protocolError interface{ StableProtocolCode() string }
	if !errors.As(err, &protocolError) {
		return false
	}
	switch protocolError.StableProtocolCode() {
	case "CONTENT_REFERENCE_MISSING", "CONTENT_REFERENCE_CORRUPT", "CONTENT_BLOB_MISSING", "CONTENT_BLOB_CORRUPT":
		return true
	default:
		return false
	}
}

func pad(value string, width int) string {
	missing := width - ansi.StringWidth(value)
	if missing > 0 {
		return value + strings.Repeat(" ", missing)
	}
	return value
}
