`default_nettype none
// AESA Digital Brain v2: ADAR1000-format SPI frame engine.
// Frame (24 bits, MSB first): {R/W, AD1, AD0, A12..A0, D7..D0}
//   AD1:AD0 = chip address (ADDR1/ADDR0 pins), header bits[14:11]=0001 = write to all chips.
// Registers written with wr + sel:
//   0 data byte       1 header low (A7..A0)      2 header high (R/W, AD1, AD0, A12..A8)
//   3 bank[ptr]=din, ptr++                       4 ptr = din[3:0]
//   5 cfg: [1:0] stride-1, [3:2] auto load after burst (bit2 RX, bit3 TX),
//          [4] cpha, [6:5] SCLK (0: clk/10, 1: clk/4, 2: clk/50, 3: clk/100)
//   6 load pulse now: din[0] RX_LOAD, din[1] TX_LOAD
// start_single: one frame {header, data byte} (read when header bit15=1).
// start_burst : 16 write frames, k = 0..15: chip = k[3:2],
//               address = header[12:0] + k[1:0]*stride, data = bank[k].
module aesa_brain (
    input  wire       clk,
    input  wire       rst,
    input  wire       wr,
    input  wire       start_single,
    input  wire       start_burst,
    input  wire [2:0] sel,
    input  wire [7:0] din,
    input  wire       miso,
    output wire       sclk,
    output wire       mosi,
    output wire       cs_n,
    output reg        busy,
    output reg        done_sticky,
    output reg        rx_load,
    output reg        tx_load,
    output wire [7:0] rx_byte
);
    reg [7:0]  data_byte;
    reg [15:0] hdr;
    reg [7:0]  bank [0:15];
    reg [3:0]  ptr;
    reg [7:0]  cfg;
    integer i;

    wire       cpha      = cfg[4];
    wire [2:0] stride    = {1'b0, cfg[1:0]} + 3'd1;
    wire [1:0] auto_load = cfg[3:2];
    reg  [5:0] half_div;
    always @(*) begin
        case (cfg[6:5])
            2'd0:    half_div = 6'd5;
            2'd1:    half_div = 6'd2;
            2'd2:    half_div = 6'd25;
            default: half_div = 6'd50;
        endcase
    end

    localparam C_IDLE = 2'd0, C_SEND = 2'd1, C_WAIT = 2'd2, C_LOAD = 2'd3;
    reg  [1:0] cst;
    reg        burst;
    reg  [3:0] k;
    reg        spi_start;
    reg  [1:0] lsel;
    reg  [3:0] ltmr;
    wire       spi_busy, spi_done;

    wire [4:0]  off         = k[1:0] * stride;
    wire [12:0] addr        = hdr[12:0] + {8'd0, off};
    wire [23:0] burst_frame = {1'b0, k[3:2], addr, bank[k]};
    wire [23:0] spi_tx      = burst ? burst_frame : {hdr, data_byte};
    wire        _unused     = &{spi_busy, cfg[7], 1'b0};   // cfg[7] reserved

    spi_master spi (
        .clk(clk), .rst(rst), .start(spi_start), .cpha(cpha), .half_div(half_div),
        .tx(spi_tx), .miso(miso), .sclk(sclk), .mosi(mosi), .cs_n(cs_n),
        .busy(spi_busy), .done(spi_done), .rx_byte(rx_byte)
    );

    always @(posedge clk) begin
        if (rst) begin
            data_byte <= 8'd0; hdr <= 16'd0; ptr <= 4'd0; cfg <= 8'd0;
            cst <= C_IDLE; burst <= 1'b0; k <= 4'd0; spi_start <= 1'b0;
            busy <= 1'b0; done_sticky <= 1'b0; rx_load <= 1'b0; tx_load <= 1'b0;
            lsel <= 2'd0; ltmr <= 4'd0;
            for (i = 0; i < 16; i = i + 1) bank[i] <= 8'd0;
        end else begin
            spi_start <= 1'b0;
            case (cst)
                C_IDLE: begin
                    if (wr) begin
                        case (sel)
                            3'd0: data_byte <= din;
                            3'd1: hdr[7:0]  <= din;
                            3'd2: hdr[15:8] <= din;
                            3'd3: begin bank[ptr] <= din; ptr <= ptr + 4'd1; end
                            3'd4: ptr <= din[3:0];
                            3'd5: cfg <= din;
                            3'd6: if (din[1:0] != 2'd0) begin
                                      lsel <= din[1:0]; ltmr <= 4'd0; busy <= 1'b1;
                                      done_sticky <= 1'b0; cst <= C_LOAD;
                                  end
                            default: ;
                        endcase
                    end else if (start_single || start_burst) begin
                        burst <= start_burst; k <= 4'd0; busy <= 1'b1;
                        done_sticky <= 1'b0; cst <= C_SEND;
                    end
                end
                C_SEND: begin spi_start <= 1'b1; cst <= C_WAIT; end
                C_WAIT: if (spi_done) begin
                    if (burst && k != 4'd15) begin k <= k + 4'd1; cst <= C_SEND; end
                    else if (burst && auto_load != 2'd0) begin
                        lsel <= auto_load; ltmr <= 4'd0; cst <= C_LOAD;
                    end else begin busy <= 1'b0; done_sticky <= 1'b1; cst <= C_IDLE; end
                end
                C_LOAD: begin                       // ~15-cycle load pulse (300 ns at 50 MHz)
                    rx_load <= lsel[0]; tx_load <= lsel[1]; ltmr <= ltmr + 4'd1;
                    if (ltmr == 4'd15) begin
                        rx_load <= 1'b0; tx_load <= 1'b0; busy <= 1'b0;
                        done_sticky <= 1'b1; cst <= C_IDLE;
                    end
                end
                default: cst <= C_IDLE;
            endcase
        end
    end
endmodule
