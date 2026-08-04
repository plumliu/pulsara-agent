package presentation

import "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"

func cloneHistoryCells(values []protocolvalue.HistoryCell) []protocolvalue.HistoryCell {
	return append([]protocolvalue.HistoryCell(nil), values...)
}

func cloneOperationalCells(values []protocolvalue.OperationalCell) []protocolvalue.OperationalCell {
	return append([]protocolvalue.OperationalCell(nil), values...)
}
