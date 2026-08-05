package client

import (
	"errors"
	"fmt"
	"time"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/app"
	"github.com/plumliu/pulsara-agent/clients/terminal/internal/commandstate"
	terminalwire "github.com/plumliu/pulsara-agent/clients/terminal/internal/wire"
)

func (s *Service) mutation(token app.OperationToken, candidate commandstate.Candidate) (commandstate.Outcome, error) {
	request, err := candidate.ToProto(token.RequestID)
	if err != nil || candidate.Binding().AttachmentID != s.attachment.ID || candidate.Binding().AttachmentGeneration != s.attachment.Generation || candidate.Binding().RuntimeSessionID != s.attachment.RuntimeSessionID || candidate.Binding().ExpectedControllerGeneration != s.attachment.ControllerGeneration {
		return commandstate.Outcome{}, errors.New("terminal mutation candidate authority is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.MutationFrame(request), 30*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return commandstate.Outcome{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "command_outcome"); err != nil {
		return commandstate.Outcome{}, err
	}
	outcome, err := commandstate.OutcomeFromProto(response.GetCommandOutcome())
	if err != nil {
		return commandstate.Outcome{}, fmt.Errorf("validate terminal mutation outcome: %w", err)
	}
	if outcome.RequestID != token.RequestID {
		return commandstate.Outcome{}, errors.New("terminal mutation outcome request identity is stale")
	}
	if outcome.CommandID != candidate.ID() {
		return commandstate.Outcome{}, errors.New("terminal mutation outcome crosses candidate authority")
	}
	return outcome, nil
}

func (s *Service) queryCommand(token app.OperationToken, candidate commandstate.Candidate) (commandstate.QueryResult, error) {
	request, err := candidate.QueryToProto(token.RequestID)
	if err != nil || candidate.Binding().RuntimeSessionID != s.attachment.RuntimeSessionID {
		return commandstate.QueryResult{}, errors.New("terminal command query authority is stale")
	}
	response, err := s.connection.RoundTrip(terminalwire.QueryCommandFrame(request), 10*time.Second, token.OperationID, token.OperationGeneration)
	if err != nil {
		return commandstate.QueryResult{}, err
	}
	if err := terminalwire.ValidateServerFrame(response, "query_command"); err != nil {
		return commandstate.QueryResult{}, err
	}
	result, err := commandstate.QueryResultFromProto(response.GetQueryCommand())
	if err != nil || result.RequestID != token.RequestID || (result.Found && result.Outcome.CommandID != candidate.ID()) {
		return commandstate.QueryResult{}, errors.New("terminal command query result crosses candidate authority")
	}
	return result, nil
}
