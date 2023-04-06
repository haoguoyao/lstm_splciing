import os
import torch
from torch import nn
import numpy as np
import torch.nn.functional as F
from torchvision import transforms
from torch.utils.data import DataLoader, random_split
from load_raw_data import SPLICEBERT_PATH
import pytorch_lightning as pl
from args import args
from scipy.stats import spearmanr, pearsonr
from sklearn.metrics import average_precision_score
import scipy.sparse as ss
from torchmetrics.classification import BinaryAUROC,BinaryF1Score
from torcheval.metrics.functional import binary_auprc
from transformers import AutoTokenizer, AutoModel, AutoModelForMaskedLM, AutoModelForTokenClassification
import matplotlib.pyplot as pyplot

class ResidualBlock(pl.LightningModule):
    def __init__(self,in_channel,out_channel,kernel_size = 11,dilation = 1):
        # super(ResidualBlock,self).__init__()
        super().__init__()
        self.in_channel = in_channel
        self.out_channel = out_channel
        self.conv1ds =[torch.nn.Conv1d(in_channel,in_channel,kernel_size,padding = 'same',dilation=dilation).cuda() for i in range (0,1)]
        
        self.conv1d2 = torch.nn.Conv1d(in_channel,out_channel,kernel_size,padding = 'same',dilation=dilation)
        self.relu = torch.nn.ReLU()
        self.batch_normalization1 = torch.nn.BatchNorm1d(in_channel)
        self.pool = nn.MaxPool1d(2, stride=2)
        self.conv1dpool =torch.nn.Conv1d(in_channel,out_channel,2,stride = 2)
        self.batch_normalization2 = torch.nn.BatchNorm1d(out_channel)
        
        
    def forward(self,x):
        for i in range(0,len(self.conv1ds)):
            # print(i)
            out = self.conv1ds[i](x)
            out = self.relu(out)
            out =self.batch_normalization1(out)
            x = out+x
        return x


    
    
class Single_site_model(pl.LightningModule):
    def __init__(self,input_length,input_size,hidden_size,num_layers=3, dropout=None):
        super().__init__() 
        if args.single_site_type=="RNN":
            self.single_site_module = RNN_module(input_size,hidden_size,num_layers)
            # self.linear1 = nn.Linear(hidden_size*input_length,1)
        if args.single_site_type=="SpliceBERT":
            self.single_site_module = SpliceBert_module()
        # self.single_site_module = nn.GRU(input_size, hidden_size, num_layers,batch_first=True)
        self.linear1 = nn.Linear(hidden_size*input_length,1)
        self.sigmoid = nn.Sigmoid()
        self.dropout = nn.Dropout(p=dropout)

    def forward_single_site_model(self,x):

        DNA_seq = x["DNA_seq"]
        histone_mark = x["histone_mark"]
        raw_seq = x["raw_seq"]
        if args.single_site_type=="RNN":
            rnn_input = torch.concatenate((histone_mark, DNA_seq), axis = 1)
            rnn_input = torch.transpose(rnn_input, 1, 2)
            return self.single_site_module(rnn_input)
        if args.single_site_type=="SpliceBERT":
            return self.single_site_module(raw_seq,histone_mark)
        return None
        
    def forward(self,x):
        output = self.forward_single_site_model(x)
        output = torch.flatten(output,start_dim=1)
        output = self.dropout(output)
        output = self.linear1(output)
        output = self.sigmoid(output)
        return output
    

class Self_attention(nn.Module):
    
    def __init__(self,embed_dim, num_heads):
        super(Self_attention, self).__init__()
        self.num_heads = num_heads
        self.embed_dim = embed_dim
        self.head_dim = embed_dim // num_heads

        
        assert (self.head_dim*num_heads==embed_dim)
        
        self.Q_linear= nn.Linear(self.embed_dim, self.head_dim*num_heads, bias = False)
        self.K_linear = nn.Linear(self.embed_dim, self.head_dim*num_heads, bias = False)
        self.V_linear = nn.Linear(self.embed_dim, self.head_dim*num_heads, bias = False)
        # self.V_relative_linear = nn.Linear(1, self.head_dim*num_heads, bias = False)
        self.K_relative_linear = nn.Linear(1, self.head_dim*num_heads, bias = False)
        self.V_absolute_linear = nn.Linear(1, self.head_dim*num_heads, bias = False)
        self.K_absolute_linear = nn.Linear(1, self.head_dim*num_heads, bias = False)
        self.fc_out = nn.Linear(embed_dim,embed_dim)
        self.absolute_position = args.absolute_position
        self.relative_position = args.relative_position


    def forward(self,V,K,Q,position = None):
        
        # v.shape batch_size,length,dimention 
        
        N = 1  #how many example in one batch
        
        V_len, K_len, Q_len = V.shape[0], K.shape[0], Q.shape[0]
        

        V = self.V_linear(V)
        K = self.K_linear(K)
        Q = self.Q_linear(Q)
        V = V.reshape(N, V_len, self.num_heads, self.head_dim)
        K = K.reshape(N, K_len,self.num_heads, self.head_dim)
        Q = Q.reshape(N, Q_len,self.num_heads, self.head_dim)
        if self.absolute_position:
            absolute_position = torch.log(position+1)/10
            absolute_position = position.reshape(N, Q_len,1)
            absolute_position_V = self.V_absolute_linear(absolute_position)
            absolute_position_V = absolute_position_V.reshape(N, V_len, self.num_heads, self.head_dim)
            absolute_position_K = self.K_absolute_linear(absolute_position)
            absolute_position_K = absolute_position_K.reshape(N, Q_len, self.num_heads, self.head_dim)
            V = V + absolute_position_V
            K = K + absolute_position_K
           
            
        if self.relative_position:
            temp1 = position.reshape(N,Q_len,1)
            temp2 = position.reshape(N,1,K_len)
            relative_position = torch.abs(temp1-temp2)
            relative_position = torch.log(relative_position+1)
            relative_position = relative_position.reshape(N, Q_len, K_len,1)
            # relative_position_V = self.V_relative_linear(relative_position)
            # relative_position_V = relative_position_V.reshape(N, Q_len, K_len,self.num_heads, self.head_dim)
            # relative_position_V = relative_position_V.permute(0,3,1,2,4)
            relative_position_K = self.K_relative_linear(relative_position)
            relative_position_K = relative_position_K.reshape(N, Q_len, K_len,self.num_heads, self.head_dim)
            relative_position_K = relative_position_K.permute(0,3,1,2,4)
            # shape: (N, num_head, Q_len, K_len, head_dims)
            

        energy = torch.einsum("nqhd,nkhd->nhqk",[Q,K])
        if self.relative_position:
            attention_relation = torch.einsum("nqhd,nhqjd->nhqj",[Q,relative_position_K])

            energy = energy+attention_relation
            

        #query shape:(N, Q_len, num_head, head_dims)
        #key shape:(N, K_len, num_head, head_dims)
        #energy shape:(N, num_heads, Q_len, K_len)
        attention = torch.softmax(energy / (self.embed_dim**(1/2)), dim=3)
        
        #out shape:(N, Q_len, num_head, head_dims)
        out = torch.einsum("nhql,nlhd->nqhd",[attention, V])
        
        # if self.relative_position:
        #     attention = attention.reshape(N,self.num_heads, Q_len, K_len,1)
        #     add_relative_V = attention*relative_position_V
        #     out = out+(add_relative_V).sum(dim=3).permute(0,2,1,3)


        #attention shape:(N,num_heads, Q_len, K_len)
        #value shape:(N, V_len, num_heads, head_dims)
        #out shape:(N, Q_len, num_head, head_dims)
        out = out.reshape(N,Q_len,self.embed_dim)
     
        out = self.fc_out(out)
        out = torch.squeeze(out)
        # print(torch.einsum("nqhd,nhqjd->nhqj",[Q,relative_position_K]).shape)
        return out,None




class RNN_module(pl.LightningModule):
    def __init__(self,input_size,hidden_size,num_layers=3):
        super().__init__() 
        self.rnn = nn.GRU(input_size,hidden_size,num_layers,batch_first=True)
        
    def forward(self,x):

        output, hn = self.rnn(x)
        return output


class SpliceBert_module(pl.LightningModule):
    def __init__(self):
        super().__init__() 
        self.model = AutoModel.from_pretrained(SPLICEBERT_PATH) 
        
    def forward(self,raw_seq,histone_mark):
    # with torch.no_grad():
        # input (batch_size, 512)
        last_hidden_state = self.model(raw_seq).last_hidden_state # get hidden states from last layer
    #output (batch size, 512, 512)
        if args.histone=="all":
            last_hidden_state = torch.cat((last_hidden_state,torch.transpose(histone_mark, 1, 2)),dim=2)

        

    # hiddens_states = model(input_ids, output_hidden_states=True).hidden_states
        return last_hidden_state

    
class Multi_site_model(pl.LightningModule):
    def __init__(self,input_length,input_size,hidden_size,num_layers=3,dropout=0):
        

        super().__init__()
        self.save_hyperparameters()
        if args.single_site_type=="RNN":
            self.single_site_module = RNN_module(input_size,hidden_size,num_layers)
        if args.single_site_type=="SpliceBERT":
            self.single_site_module = SpliceBert_module()
        
        self.dropout = nn.Dropout(p=dropout)
        outer_rnn_input_size = 8*input_length
        outer_rnn_hidden_size = 8*input_length
        self.outer_rnn_module = RNN_module(outer_rnn_input_size,outer_rnn_hidden_size,num_layers=1)
        self.sigmoid = nn.Sigmoid()
        self.linear1 = nn.Linear(hidden_size*input_length,outer_rnn_input_size)
        self.linear = nn.Linear(outer_rnn_hidden_size,1)
        self.attention = Self_attention(embed_dim = outer_rnn_hidden_size, num_heads = args.num_head)
        self.layer_norm = nn.LayerNorm(outer_rnn_hidden_size)

    def forward_single_site_model(self,x):
        DNA_seq = x["DNA_seq"].squeeze()
        histone_mark = x["histone_mark"].squeeze()
        raw_seq = x["raw_seq"].squeeze()
        if args.single_site_type=="RNN":
            rnn_input = torch.concatenate((histone_mark, DNA_seq), axis = 1)
            rnn_input = torch.transpose(rnn_input, 1, 2)
            return self.single_site_module(rnn_input)
        elif args.single_site_type=="SpliceBERT":
            return self.single_site_module(raw_seq,histone_mark)

        return 1/0



    def forward(self,x):

        
        # print("multi-site model")
        # x_shape_list = list(x.shape)
        #[1,num,19,512]
        position = x["position"]
        # print(1/0)

        x = self.forward_single_site_model(x)
        x = torch.flatten(x,start_dim=1)
        x = self.linear1(x)
        x = self.outer_rnn_module(x)

        # attention,weights = self.attention(V = x, K = x,Q = x, position = position)      
        # # TODO
        # x = x+attention
        # x = self.layer_norm(x)  

        x = self.dropout(x)
        x = self.linear(x)
        x = self.sigmoid(x)
        return x
    # def forward_visualization(self,x,position,seq):
    #     # print("multi-site model")
    #     # x_shape_list = list(x.shape)
        
    #     x = torch.squeeze(x)
    #     x = torch.transpose(x, 1, 2)
    #     x = self.forward_single_site_model(x,seq)
    #     x = torch.flatten(x,start_dim=1)
    #     # x = self.linear1(x)
    #     x = self.outer_rnn_module(x)

    #     # attention,weights = self.attention(V = x, K = x,Q = x, position = position)      
    #     #TODO
    #     # x = x+attention
    #     # x = self.layer_norm(x)    

    #     x = self.dropout(x)
    #     x = self.linear(x)
    #     x = self.sigmoid(x)
    #     return x,weights
    

# class CNN_module(pl.LightningModule):
#     def __init__(self, conv_channel, W, AR, in_channels, out_channels,dropout=None):
#         # super(CNN_module,self).__init__()
#         super().__init__()

#         self.conv1 = nn.Conv1d(args.input_channel,conv_channel,1,padding = 'same')
#         self.conv2 = nn.Conv1d(conv_channel,conv_channel,1,padding = 'same')
#         self.residual_blocks = [ResidualBlock(in_channel, out_channel, a, b).cuda() for in_channel, out_channel,a, b in zip(in_channels, out_channels, W, AR)]
        
        
#         self.conv1x1s = [nn.Conv1d(conv_channel,conv_channel,2,stride = 2).cuda()]
#         self.convs = [nn.Conv1d(conv_channel,conv_channel,1,padding = 'same').cuda() for i in range(len(W)) if (((i+1) % 4 == 0) or ((i+1) == len(W)))]
        
        
#         if args.device=='cuda':
#             for i in self.residual_blocks:
#                 i.cuda()
#             for j in self.convs:
#                 j.cuda()

#         self.batch_normalization = torch.nn.BatchNorm1d(conv_channel)
#         self.relu = torch.nn.ReLU()
#         self.conv_final = nn.Conv1d(out_channels[-1],out_channels[-1],2,padding = 'same')
#         self.linear1 = nn.Linear(2*out_channels[-1],1)
#         self.pool = nn.MaxPool1d(2, stride=2)
#         self.pool3 = nn.MaxPool1d(3, stride=1)
#         self.sigmoid = nn.Sigmoid()
#     def forward(self, x):
#         x = self.conv1(x)
#         skip = self.conv2(x)
#         idx = 0
#         for i, cnn in enumerate(self.residual_blocks):
#             x = cnn(x)

#         x = self.conv_final(x)

#         x = self.pool(x)
#         x = torch.flatten(x,start_dim=1)
#         x = self.linear1(x)
#         x = self.sigmoid(x)
#         return x
        
# class CNN_module2(pl.LightningModule):
#     def __init__(self, conv_channel, W, AR, in_channels, out_channels,dropout=None):
#         # super(CNN_module,self).__init__()
#         super().__init__()

#         self.conv1 = nn.Conv1d(args.input_channel,conv_channel,1,padding = 'same')
#         self.conv2 = nn.Conv1d(conv_channel,conv_channel,1,padding = 'same')
#         self.residual_blocks = [ResidualBlock(in_channel, out_channel, a, b).cuda() for in_channel, out_channel,a, b in zip(in_channels, out_channels, W, AR)]
        
        
#         self.conv1x1s = [nn.Conv1d(conv_channel,conv_channel,2,stride = 2).cuda()]
#         self.convs = [nn.Conv1d(conv_channel,conv_channel,1,padding = 'same').cuda() for i in range(len(W)) if (((i+1) % 4 == 0) or ((i+1) == len(W)))]
        
        
#         if args.device=='cuda':
#             for i in self.residual_blocks:
#                 i.cuda()
#             for j in self.convs:
#                 j.cuda()

#         self.batch_normalization = torch.nn.BatchNorm1d(conv_channel)
#         self.relu = torch.nn.ReLU()
#         self.conv_final = nn.Conv1d(out_channels[-1],3,1,padding = 'same')
#         self.linear1 = nn.Linear(3*512,1)
#         self.sigmoid = nn.Sigmoid()
        
#     def forward(self, x):
#         x = self.conv1(x)
#         skip = self.conv2(x)
#         idx = 0
#         for i, cnn in enumerate(self.residual_blocks):
#             x = cnn(x)
#         x = self.conv_final(x)
#         x = torch.flatten(x,start_dim=1)
#         x = self.linear1(x)
#         x = self.sigmoid(x)
#         return x

class Lightning_module(pl.LightningModule):
    def __init__(self,model,task,model_type):
        super().__init__()
        
        self.model = model
        self.model.cuda()

        self.loss_func = nn.BCELoss()
        self.task=task
        if model_type=="single":
            self.multi_model=False
        if model_type=="multi":
            self.multi_model=True
        return
    
    def cross_entropy(self,eps=1e-10):
        def a(y_pred, y_true):
            assert len(y_pred.shape) == len(y_true.shape)
            
            loss = torch.mean(torch.sum(-y_true*torch.log(y_pred+eps),dim=-1))
            return loss
        return a

    
    def on_train_start(self):
        print("-----------log hparams-------------")
        self.logger.log_hyperparams(vars(args))
        # self.draw_graph()
    
    #log the computational graph at the beginning of the training
    def forward(self,x,position = None,seq = None):

        return self.model.forward(x)
    # def forward_visualization(self,x,position = None,seq = None):
    #     if self.multi_model:
    #         return self.model.forward_visualization(x,position,seq)
    #     else:
    #         print("single model, cannot visualize")
    #         return None

    def get_correlation(self,y_true, y_pred):
        y_true= np.copy(y_true)
        y_pred_np = np.copy(y_pred)
        
        y_true[np.isnan(y_true)] = -1

        rho = []
        pearson = []
        num_idx_true = []

        for psi_t in [0, 0.1, 0.2, 0.3]:
            idx_true = np.nonzero(np.logical_and(y_true >= psi_t, y_true < 1.0-psi_t))

            idx_true = idx_true[0]
            rho1, pval1 = spearmanr(y_true[idx_true], y_pred[idx_true])
            rho1 = round(rho1,4)
            if y_true[idx_true].shape[0]>1:
                rho2,_ = pearsonr(y_true[idx_true], y_pred[idx_true])
                rho2 = round(rho2,4)
            else:
                rho2 = None
                
            rho.append(rho1)
            pearson.append(rho2)
            num_idx_true.append(np.size(idx_true))
        return rho,pearson, num_idx_true
    


    def print_on_epoch_end(self,validation_step_outputs):
        
        Y_pred_lst =[]
        Y_true_lst =[]
        for item in validation_step_outputs:
            Y_pred_lst.append(item['y_hat'])
            Y_true_lst.append(item['y'])
        Y_pred = torch.cat(Y_pred_lst, dim=0)
        Y_true = torch.cat(Y_true_lst, dim=0)


        #make the last dimention the 3 classes
        # print(Y_pred.shape)
        # print(Y_true.shape)
        return

#         Y_true_acceptor =Y_true[:,:,1].flatten()
#         Y_pred_acceptor =Y_pred[:,:,1].flatten()
#         print(Y_pred[:,:,0].flatten())
#         print(Y_pred_acceptor.shape)
#         print("log auprc, topk1_acc")
#         topk1_acc_a, auprc_a, threshold_a, num_idx_true_a = self.get_top1_statistics(np.asarray(Y_true_acceptor), np.asarray(Y_pred_acceptor))
#         print("Acceptor: topk1_acc {} auprc {} threshold {} num_idx_true {}".format(topk1_acc_a, auprc_a, threshold_a, num_idx_true_a))
#         rho_a, num_idx_true_a2 = self.get_correlation(np.asarray(Y_true_acceptor), np.asarray(Y_pred_acceptor))
#         print("Acceptor correlation: rho {} num_idx_true {}".format(rho_a, num_idx_true_a2))
        
        
#         return rho_a
    
    def _accuracy(self,result,target):
        count = 0.0
        for i in range(0,len(result)):
            if target[i]>0.5 and result[i]>0.5:
                count+=1
            elif target[i]<0.5 and result[i]<0.5:
                count+=1 
        return count/len(result)
    
    def evaluate_site_cls(self,step_outputs, step_y):
        print("Accuracy {:.6}".format(self._accuracy(step_outputs,step_y)))
        binaryAUROC = BinaryAUROC(thresholds=None)
        binaryF1 = BinaryF1Score()
        print("Binary F1 {:.6}".format(binaryF1(torch.from_numpy(step_outputs), torch.from_numpy(step_y))))
        print("Binary AUROC {:.6}".format(binaryAUROC(torch.from_numpy(step_outputs), torch.from_numpy(step_y))))
        
        print("Binary AUPRC {:.6}".format(binary_auprc(torch.from_numpy(step_outputs), torch.from_numpy(step_y))))
        return
        
    def evaluate(self,step_outputs,step_y):
        if self.task=="reg":
            return self.evaluate_site_reg(step_outputs,step_y)
        if self.task=="cls":
            return self.evaluate_site_cls(step_outputs,step_y)

    def evaluate_site_reg(self,step_outputs,step_y):

        rho,pearson, num_idx_true = self.get_correlation(step_y,step_outputs)
        print("spearman correlation: rho {} num_idx_true {}".format(rho, num_idx_true))
        print("pearson correlation: rho {} num_idx_true {}".format(pearson, num_idx_true))

        return


    def _epoch_end(self,step_outputs):
        # lst = []
        # lst_y= []
        y_collection = None
        y_hat_collection = None
        for i in step_outputs:
            if y_collection is None:
                y_collection = i["y"]
                y_hat_collection = i["y_hat"]
            else:
                y_collection = torch.cat((y_collection,i["y"]),dim=0)
                y_hat_collection = torch.cat((y_hat_collection,i["y_hat"]),dim=0)

            # lst+=list(np.squeeze(np.array(i["y_hat"]), axis=1))
            # lst_y+=list(np.squeeze(np.array(i["y"]),axis = 1))
        # step_outputs = np.array(lst)
        # step_y = np.array(lst_y)
        return y_hat_collection,y_collection

                    
    def validation_epoch_end(self, step_outputs):
        print("---------------validation epoch end------------------")
        step_pred,step_y= self._epoch_end(step_outputs)

        print("validation splicing site number {}".format(step_y.shape))
        print("validation evaluation ")
        loss = self.loss_func(step_pred,step_y)
        self.evaluate(step_pred.numpy().squeeze(), step_y.numpy().squeeze())
        
        # np.savetxt("valid.csv",np.vstack((step_pred,step_y)).transpose(), delimiter=",", fmt='%s')
        return {"val_loss":loss}
    
    
    
    def training_epoch_end(self,step_outputs):
        print("---------------training epoch end------------------")
        step_pred,step_y = self._epoch_end(step_outputs)

        print("training splicing site number {}".format(step_y.shape))
        # print(np.count_nonzero(step_y==0))
        print("training evaluation")
        # self.evaluate_single_site(step_outputs, step_y)
        self.evaluate(step_pred, step_y)

        return
    def test_step(self,batch,batch_idx):
        return self.validation_step(batch, batch_idx)
    def test_epoch_end(self,step_outputs):
        return self.validation_epoch_end(step_outputs)

    def training_step(self, batch, batch_idx):

        x, y= batch['x'],batch['y']
        if self.multi_model:
            # print("multi-model")

            # y_hat = self(x,position = position,seq = seq)
            y_hat = self.forward(x)
            y = torch.transpose(y, 0, 1)
            loss = self.loss_func(y_hat, y)*y.shape[0]
        else:
            y_hat = self(x)
            # y_hat = self.forward(x,position = position,seq = seq)
            y = y[:, None]
            loss = self.loss_func(y_hat, y)

        self.log("train_loss_step", loss,on_step=True, on_epoch=False, prog_bar=True)
        self.log("train_loss", loss,on_step=False, on_epoch=True, prog_bar=True)
        
        # step learning rate scheduler
        self.lr_schedulers().step()
        return {"loss": loss, "y_hat": y_hat.detach().to("cpu"),"y":y.to("cpu")}

    def validation_step(self, batch, batch_idx):

        x, y= batch['x'],batch['y']
        if self.multi_model:
            # y_hat = self(x,position = position,seq = seq)
            y_hat = self.forward(x)
            y = torch.transpose(y, 0, 1)
            loss = self.loss_func(y_hat, y)*y.shape[0]
        else:
            y_hat = self(x)
            # y_hat = self.forward(x,position = position,seq = seq)
            y = y[:, None]
            loss = self.loss_func(y_hat, y)
        
        # loss = self.loss_func(y_hat, y)
        
        self.log("validation_loss_step", loss,on_step=True, on_epoch=False, prog_bar=True)
        self.log("validation_loss", loss,on_step=False, on_epoch=True, prog_bar=True)
        return {"loss": loss, "y_hat": y_hat.detach().to("cpu"),"y":y.to("cpu")}
        
        
    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=args.learning_rate)
        # optimizer = torch.optim.SGD(self.parameters(), lr=args.learning_rate, momentum=0.9)
        scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[25000,50000,100000], gamma=0.2)
        return [optimizer], [scheduler]