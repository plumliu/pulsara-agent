package presentation

import (
	"sort"

	"github.com/plumliu/pulsara-agent/clients/terminal/internal/protocolvalue"
)

func sortHistoryCells(values []protocolvalue.HistoryCell) {
	sort.Slice(values, func(i, j int) bool {
		if values[i].DisplayRank != values[j].DisplayRank {
			return values[i].DisplayRank < values[j].DisplayRank
		}
		return values[i].ID < values[j].ID
	})
}
